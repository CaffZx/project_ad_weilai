import pymysql
from pymysql.cursors import DictCursor

HOSTS = ["36.133.119.99", "172.17.0.16", "127.0.0.1", "localhost"]
ASIN = "B0GCZHVBWN"
SHOP = "am_vivibeautyus"

for h in HOSTS:
    try:
        c = pymysql.connect(
            host=h,
            port=9030,
            user="cbr",
            password="cbr123",
            database="x_dwd",
            connect_timeout=5,
            cursorclass=DictCursor,
        )
        cur = c.cursor()
        for label, sql, params in [
            ("with_shop", "AND s.account = %s", (ASIN, ASIN, SHOP)),
            ("any_shop", "", (ASIN, ASIN)),
        ]:
            cur.execute(
                f"""
                SELECT a.parent_asin, a.parent_seller_sku, s.account AS shop_account
                FROM dwd_whp_amazon_listing_general a
                JOIN dwd_shop s ON a.shop_id = s.id
                WHERE (a.parent_asin = %s OR a.asin = %s)
                  AND a.parent_seller_sku IS NOT NULL AND a.parent_seller_sku != ''
                  {sql}
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
            print(f"{h}/{label}: {row}")
        c.close()
    except Exception as e:
        print(f"{h}: FAIL {e}")
