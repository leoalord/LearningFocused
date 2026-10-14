"""Static checks on the Postgres VM deploy files (no GCP calls)."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class TestPostgresDeployFiles(unittest.TestCase):
    def test_cloud_init_parses_and_defines_units(self) -> None:
        text = (ROOT / "deploy/postgres/cloud-init.yaml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#cloud-config"))
        doc = yaml.safe_load(text)
        paths = {f["path"] for f in doc["write_files"]}
        self.assertEqual(
            paths,
            {
                "/etc/systemd/system/pgdata-mount.service",
                "/etc/systemd/system/learningfocused-postgres.service",
                "/etc/systemd/system/pg-backup.service",
                "/etc/systemd/system/pg-backup.timer",
            },
        )
        for placeholder in ("__IMAGE__", "__BUCKET__", "__PROJECT__", "__SECRET__"):
            self.assertIn(placeholder, text)

    def test_deploy_script_fills_every_placeholder(self) -> None:
        script = (ROOT / "scripts/deploy_postgres_vm.sh").read_text(encoding="utf-8")
        for placeholder in ("__IMAGE__", "__BUCKET__", "__PROJECT__", "__SECRET__"):
            self.assertIn(f"s#{placeholder}#", script)
        self.assertIn("--no-address", script)
        self.assertNotIn("inferpoker", script)

    def test_image_preloads_age(self) -> None:
        dockerfile = (ROOT / "deploy/postgres/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("postgresql-16-age", dockerfile)
        self.assertIn("shared_preload_libraries=age", dockerfile)


if __name__ == "__main__":
    unittest.main()
