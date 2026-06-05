"""Run Campaign KB + Wizard + ERP write for each ASIN sequentially."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import AD_DIRECTION_AGENT, ERP_DB_DIR, REPO_ROOT, SCRIPTS_DIR, bootstrap_sys_path

bootstrap_sys_path()

_ROOT = REPO_ROOT
_OUT = _ROOT / "cursor临时文件"


def _script_path(name: str) -> Path:
    return SCRIPTS_DIR / name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("erp_batch")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--asins", required=True, help="Comma-separated parent ASINs")
    p.add_argument("--output-dir", default=str(_OUT))
    p.add_argument("--skip-campaign", action="store_true")
    p.add_argument("--skip-wizard", action="store_true")
    p.add_argument("--skip-preset", action="store_true")
    p.add_argument("--erp-only", action="store_true", help="Only write_erp_all (kb+wizard must exist)")
    return p.parse_args()


def _run_script(path: Path, *argv: str) -> int:
    cmd = [sys.executable, str(path), *argv]
    logger.info("CMD: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=AD_DIRECTION_AGENT)
    return proc.returncode


def _run_scripts_dir(script: str, *argv: str) -> int:
    return _run_script(_script_path(script), *argv)


def _run_erp_db(script: str, *argv: str) -> int:
    return _run_script(ERP_DB_DIR / script, *argv)


async def _campaign_one(asin: str, kb_path: Path) -> int:
    if kb_path.exists():
        kb_path.unlink()
    return await asyncio.to_thread(
        _run_scripts_dir,
        "campaign_kb_experiment.py",
        "--asins",
        asin,
        "--temperatures",
        "0.0",
        "--runs",
        "1",
        "--output",
        str(kb_path),
    )


async def _wizard_one(asin: str, out_dir: Path) -> int:
    return await asyncio.to_thread(
        _run_scripts_dir,
        "wizard_kb_experiment.py",
        "--asins",
        asin,
        "--output-dir",
        str(out_dir),
        "--days",
        "7",
    )


def _erp_write(asin: str, kb_path: Path, wizard_path: Path, report_path: Path) -> int:
    argv = ["--asin", asin, "--kb", str(kb_path), "--report", str(report_path)]
    if wizard_path.exists():
        argv.extend(["--wizard", str(wizard_path)])
    return _run_erp_db("write_erp_all.py", *argv)


def _kb_ok(kb_path: Path) -> bool:
    if not kb_path.exists():
        return False
    try:
        for line in kb_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            adj = obj.get("adjustments") or []
            return len(adj) > 0 and obj.get("sanity_check_passed", True) is not False
    except Exception:
        return False
    return False


async def main() -> int:
    args = _parse_args()
    asins = [a.strip().upper() for a in args.asins.split(",") if a.strip()]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    status_path = out_dir / "erp_batch_sequential_status.json"

    if not args.skip_preset and not args.erp_only:
        rc = _run_scripts_dir("apply_strategy_preset.py", "--asins", ",".join(asins))
        if rc != 0:
            logger.error("strategy preset failed rc=%s", rc)
            return rc

    results: list[dict] = []
    failures = 0

    for i, asin in enumerate(asins, start=1):
        logger.info("========== [%d/%d] %s ==========", i, len(asins), asin)
        kb_path = out_dir / f"{asin}_kb.jsonl"
        wizard_path = out_dir / f"{asin}_wizard.jsonl"
        report_path = out_dir / f"{asin}_write_report_batch.json"
        entry: dict = {
            "asin": asin,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ok": False,
        }

        try:
            if args.erp_only:
                pass
            elif not args.skip_campaign:
                rc = await _campaign_one(asin, kb_path)
                entry["campaign_rc"] = rc
                if rc != 0 or not _kb_ok(kb_path):
                    entry["error"] = "campaign_failed_or_empty_kb"
                    results.append(entry)
                    failures += 1
                    continue

            if not args.erp_only and not args.skip_wizard:
                rc = await _wizard_one(asin, out_dir)
                entry["wizard_rc"] = rc
                if rc != 0 or not wizard_path.exists():
                    entry["error"] = "wizard_failed"
                    results.append(entry)
                    failures += 1
                    continue

            if not _kb_ok(kb_path):
                entry["error"] = "missing_or_empty_kb"
                results.append(entry)
                failures += 1
                continue

            rc = _erp_write(asin, kb_path, wizard_path, report_path)
            entry["erp_rc"] = rc
            if rc != 0:
                entry["error"] = "erp_write_failed"
                failures += 1
            else:
                rep = json.loads(report_path.read_text(encoding="utf-8"))
                entry["decision_id"] = rep.get("write", {}).get("decision_id")
                entry["cards"] = rep.get("write", {}).get("modern_card")
                entry["ok"] = True
        except Exception as e:
            entry["error"] = str(e)
            failures += 1
            logger.exception("[%s] batch step failed", asin)

        entry["finished_at"] = datetime.now(timezone.utc).isoformat()
        results.append(entry)
        status_path.write_text(
            json.dumps(
                {"results": results, "updated_at": entry["finished_at"]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("[%s] done ok=%s %s", asin, entry.get("ok"), entry.get("error", ""))

    logger.info("BATCH DONE failures=%d/%d status=%s", failures, len(asins), status_path)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
