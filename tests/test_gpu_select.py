"""Offline tests of the GPU manager's Community Cloud choices (remote/gpu.py). Run: python -m unittest discover tests"""

import base64
import io
import os
import sys
import tarfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "remote"))
import gpu  # noqa: E402

# Community prices and stock as Runpod listed them on Oct 23, 2026 (some made up to cover the cases)
OFFERS = [
    {"id": "NVIDIA GeForce RTX 5090", "memoryInGb": 32, "price": 0.69, "stock": "Medium"},
    {"id": "NVIDIA GeForce RTX 4090", "memoryInGb": 24, "price": 0.34, "stock": "High"},
    {"id": "NVIDIA RTX A6000", "memoryInGb": 48, "price": 0.33, "stock": "Low"},
    {"id": "NVIDIA RTX PRO 5000 Blackwell", "memoryInGb": 48, "price": 0.82, "stock": "Low"},
    {"id": "NVIDIA L40S", "memoryInGb": 48, "price": 0.79, "stock": None},  # none on fast hosts right now
    {"id": "NVIDIA A100 80GB PCIe", "memoryInGb": 80, "price": 1.19, "stock": "High"},  # over $1/h
    {"id": "Tesla V100-SXM2-32GB", "memoryInGb": 32, "price": 0.2, "stock": "High"},  # no bfloat16
    {"id": "NVIDIA RTX 5000 Ada Generation", "memoryInGb": 32, "price": 0.49, "stock": "Low"},
]


class RankCommunity(unittest.TestCase):
    def test_default_order_is_cost_per_job(self):
        ids = [g for g, _ in gpu.rank_community(OFFERS)]
        self.assertEqual(ids, ["NVIDIA GeForce RTX 5090", "NVIDIA RTX 5000 Ada Generation", "NVIDIA RTX A6000",
                               "NVIDIA RTX PRO 5000 Blackwell"])

    def test_filters(self):
        ids = [g for g, _ in gpu.rank_community(OFFERS)]
        self.assertNotIn("NVIDIA GeForce RTX 4090", ids)  # 24 GB: below the 32 GB the models need
        self.assertNotIn("NVIDIA L40S", ids)  # not in stock
        self.assertNotIn("NVIDIA A100 80GB PCIe", ids)  # $1.19 is not under $1.00
        self.assertNotIn("Tesla V100-SXM2-32GB", ids)  # never used

    def test_settings(self):
        self.assertIn("NVIDIA GeForce RTX 4090", [g for g, _ in gpu.rank_community(OFFERS, min_vram_gb=24)])
        self.assertEqual([g for g, _ in gpu.rank_community(OFFERS, max_price=0.5)],
                         ["NVIDIA RTX 5000 Ada Generation", "NVIDIA RTX A6000"])
        only = ["NVIDIA RTX PRO 5000 Blackwell", "NVIDIA GeForce RTX 5090", "NVIDIA L40S"]
        self.assertEqual([g for g, _ in gpu.rank_community(OFFERS, only=only)],
                         ["NVIDIA RTX PRO 5000 Blackwell", "NVIDIA GeForce RTX 5090"])
        faster_a6000 = gpu.rank_community(OFFERS, speed={"NVIDIA RTX A6000": 1.0})
        self.assertEqual(faster_a6000[0], ("NVIDIA RTX A6000", 0.33))

    def test_nothing_fits(self):
        self.assertEqual(gpu.rank_community([]), [])
        self.assertEqual(gpu.rank_community(OFFERS, max_price=0.1), [])


class Profiles(unittest.TestCase):
    def test_profiles_for(self):
        self.assertEqual(gpu.profiles_for("minimax-h3", "flux3klein"), ["minimax-h3"])
        self.assertEqual(gpu.profiles_for("minimax-h3", "flux3klein", restyle=True), ["minimax-h3", "flux3-klein"])
        self.assertEqual(gpu.profiles_for("wan22"), ["wan22-flf", "interp"])
        self.assertEqual(gpu.profiles_for(image_model="klein4b"), ["restyle"])

    def test_profiles_exist_in_models_json(self):
        names = {p for v in (*gpu.ENGINE_PROFILES.values(), *gpu.IMAGE_PROFILES.values()) for p in v}
        self.assertTrue(gpu.model_files(sorted(names)))
        import json
        with open(os.path.join(gpu.ROOT, "setup", "models.json"), encoding="utf-8") as f:
            self.assertLessEqual(names, set(json.load(f)["profiles"]))


class PodCode(unittest.TestCase):
    def test_pod_code_has_the_pod_files_with_unix_line_ends(self):
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(gpu.pod_code())), mode="r:gz") as tar:
            self.assertEqual(sorted(tar.getnames()), sorted(gpu.POD_FILES))
            script = tar.extractfile("remote/pod_bootstrap.sh").read()
            self.assertNotIn(b"\r\n", script)
            self.assertEqual(tar.getmember("remote/pod_bootstrap.sh").mode, 0o755)


class Fallback(unittest.TestCase):
    def make(self, cloud, offers, community_pod=None):
        g = gpu.RunpodGPU.__new__(gpu.RunpodGPU)
        g.cfg = {"cloud": cloud, "max_price": 1.0, "min_vram_gb": 32, "min_download_mbps": 1000, "min_cuda": "13.0",
                 "community_models": [], "community_disk_gb": 200, "gateway_token": "x" * 24, "image": "img", "volume_id": "vol",
                 "datacenter": "EU-RO-1"}
        g.need, g.calls = [], []
        g.community_offers = lambda: offers
        g.community_start = lambda: ("args", {}, [])

        def gql(query, variables=None):
            g.calls.append(variables["i"]["gpuTypeId"])
            if community_pod is None:
                raise gpu.ApiError(400, "There are no longer any instances available")
            return {"podFindAndDeployOnDemand": {"id": community_pod, "costPerHr": 0.69}}
        g.gql = gql
        g._create_secure = lambda errors: {"id": "secure", "cloud": "SECURE", "errors": errors}
        return g

    def test_community_first(self):
        pod = self.make("COMMUNITY_FIRST", OFFERS, "abc")._create()
        self.assertEqual((pod["id"], pod["cloud"], pod["gpu_id"], pod["cost"]), ("abc", "COMMUNITY", "NVIDIA GeForce RTX 5090", 0.69))

    def test_falls_back_to_secure_when_none_is_free(self):
        g = self.make("COMMUNITY_FIRST", OFFERS)
        pod = g._create()
        self.assertEqual(pod["id"], "secure")
        self.assertEqual(len(g.calls), 4)  # tried every type that fits, cheapest per job first
        self.assertEqual(self.make("COMMUNITY_FIRST", [])._create()["id"], "secure")

    def test_community_only_never_uses_secure(self):
        with self.assertRaises(RuntimeError):
            self.make("COMMUNITY", OFFERS)._create()

    def test_secure_skips_community(self):
        g = self.make("SECURE", OFFERS, "abc")
        self.assertEqual(g._create()["id"], "secure")
        self.assertEqual(g.calls, [])


if __name__ == "__main__":
    unittest.main()
