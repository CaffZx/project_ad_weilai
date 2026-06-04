"""CLI: resolve shop_id / parent_seller_sku / site_code for parent ASIN."""
from __future__ import annotations

import argparse
import json

from _bootstrap import bootstrap_sys_path

bootstrap_sys_path()

from app.persistence.erp_writer import resolve_listing_context  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--asin", required=True)
    args = p.parse_args()
    ctx = resolve_listing_context(args.asin)
    print(
        json.dumps(
            {
                "parent_asin": ctx.parent_asin,
                "parent_seller_sku": ctx.parent_seller_sku,
                "shop_id": ctx.shop_id,
                "shop_account": ctx.shop_account,
                "site_code": ctx.site_code,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
