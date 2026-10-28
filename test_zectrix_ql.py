"""Offline regression checks; run python3 -m unittest test_zectrix_ql.py -v."""
import io
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import zectrix_ql as z


class QinglongTests(unittest.TestCase):
    def test_lunar_spring_festival_2026(self):
        from datetime import date
        lunar=z.lunar_info(date(2026,2,17))
        self.assertEqual((lunar["month"],lunar["day"]),(1,1))
        self.assertEqual(z.cell_label(date(2026,2,17)),("春节",True))
        self.assertEqual(len(z.ASSETS["lunar"]),201)
        self.assertEqual(len(z.ASSETS["solar"]),2412)

    def test_render_all_three_400x300_strict_bw(self):
        from datetime import date
        quota={"windows":[{"name":"5小时","used_percent":38,"reset_at":1800000000}],
               "credits":0,"stale":False}
        for f in (z.render_calendar(date(2026,10,21)),
                  z.render_quota(quota,{}),
                  z.render_weather(z.weather_demo("rain",2))):
            self.assertEqual(f.image.size,(400,300))
            self.assertEqual(set(f.image.get_flattened_data()),{0,255})
            self.assertTrue(f.png().startswith(b"\x89PNG"))

    def test_no_fake_quota_percent(self):
        sample={"rate_limit":{"five_hour":{"used_percent":34.3,
                 "reset_at":1800000000,"window_minutes":300},
                "weekly":{"percent_left":80,"reset_after_seconds":60}}}
        q=z.parse_usage(sample,now=1790000000)
        self.assertEqual([x["used_percent"] for x in q["windows"]],[34,20])
        with self.assertRaises(ValueError):
            z.parse_usage({"rate_limit":{"five_hour":{"used_percent":109,
                           "reset_at":1800000000}}})

    def test_oauth_and_siwc_token_rotation(self):
        ordinary={"type":"codex","access_token":"old","refresh_token":"oldr"}
        with patch.object(z,"post_form_json",return_value={
                 "access_token":"new","refresh_token":"newr","expires_in":3600}):
            updated=z.oauth_refresh(ordinary)
        self.assertEqual(updated["refresh_token"],"newr")
        self.assertEqual(ordinary["refresh_token"],"oldr")
        siwc={"type":"codex","account_id":"abc","use_siwc":True,
              "siwc":{"client_id":"c","refresh_token":"r","access_token":"a"}}
        with patch.object(z,"post_form_json",return_value={
                 "access_token":"fresh","refresh_token":"rotated","scope":"chatgpt.tokens.use.direct",
                 "expires_in":3600}):
            changed=z.oauth_refresh(siwc)
        self.assertEqual(changed["siwc"]["refresh_token"],"rotated")
        self.assertEqual(changed["siwc"]["scopes"],["chatgpt.tokens.use.direct"])
        with patch.object(z,"post_form_json",return_value={
                 "access_token":"fresh","refresh_token":"rotated","scope":"openid"}):
            with self.assertRaises(ValueError):z.oauth_refresh(siwc)

    def test_auth_refresh_writes_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"auth.json"
            path.write_text(json.dumps({"type":"codex","access_token":"a","refresh_token":"r"}))
            with patch.dict(os.environ,{"CODEX_AUTH_PATH":str(path),
                                        "CODEX_AUTH_WRITE_BACK":"true"}):
                with patch.object(z,"post_form_json",return_value={
                     "access_token":"b","refresh_token":"rotated","expires_in":3600}):
                    z.refresh_auth()
            result=json.loads(path.read_text())
            self.assertEqual(result["refresh_token"],"rotated")
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_reserved_page_is_blocked_before_upload(self):
        f=z.Frame()
        with patch.dict(os.environ,{"ZECTRIX_RESERVED_PAGES":"3",
                                    "ALLOW_PAGE_3_OVERRIDE":"false",
                                    "ZECTRIX_ALLOW_RESERVED_OVERRIDE":"false"}):
            with self.assertRaises(ValueError):z.publish(f,3,"weather")

    def test_pause_crosses_midnight(self):
        from datetime import datetime
        tz=z.TZ8
        self.assertTrue(z.in_pause_windows("23:00-06:00",datetime(2026,10,28,2,0,tzinfo=tz)))
        self.assertTrue(z.in_pause_windows("23:00-06:00",datetime(2026,10,28,23,30,tzinfo=tz)))
        self.assertFalse(z.in_pause_windows("23:00-06:00",datetime(2026,10,28,14,0,tzinfo=tz)))

    def test_open_meteo_source_validates_live_temperature(self):
        today="2026-10-28"
        fake={"current":{"temperature_2m":None,"weather_code":3},
              "hourly":{"time":[],"temperature_2m":[]},"daily":{"time":[]}}
        with patch.object(z,"get_json",return_value=fake):
            with self.assertRaises(ValueError):
                z.fetch_open_meteo({"city":"北京","district":"海淀区","lat":39.9,"lon":116.3},2)

    def test_weather_fusion_is_median_and_requires_sources(self):
        a=z.weather_demo("sunny",2)
        b=z.weather_demo("rain",2)
        merged=z.combine_weather([a,b])
        self.assertEqual(merged["temp_c"],round((a["temp_c"]+b["temp_c"])/2))
        self.assertEqual(merged["hero_icon"],"ThunderRain")
        with self.assertRaises(ValueError):z.combine_weather([])

    def test_quota_poll_also_polls_radar(self):
        args=argparse.Namespace(preview=False,output=None,force=True)
        dummy_quota={"windows":[{"name":"5小时","used_percent":20,"reset_at":1800000000}],
                     "credits":0,"stale":False}
        with patch.object(z,"in_pause_windows",return_value=False):
            with patch.object(z,"read_auth",return_value=(None,None,{"type":"codex","access_token":"access"})):
                with patch.object(z,"expiry_soon",return_value=False):
                    with patch.object(z,"fetch_quota",return_value=dummy_quota) as quota_req:
                        with patch.object(z,"fetch_radar",return_value={"data":{},"updated":1}) as radar_req:
                            with patch.object(z,"store_state"):
                                with patch.object(z,"deliver"):
                                    z.quota_task(args)
        quota_req.assert_called_once()
        radar_req.assert_called_once()

    def test_publish_multipart_protocol(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return b'{"code":0}'
        captured=[]
        def fake_urlopen(request,timeout):
            captured.append(request)
            return Response()
        with patch.dict(os.environ,{"ZECTRIX_API_KEY":"test-key",
                                    "ZECTRIX_DEVICE_ID":"AA:BB:CC",
                                    "ZECTRIX_API_BASE":"https://mock.invalid"}):
            with patch.object(z,"urlopen",side_effect=fake_urlopen):
                z.publish(z.Frame(),3,"quota")
        request=captured[0]
        self.assertIn("pageId",request.data.decode("latin1"))
        self.assertIn("image/png",request.data.decode("latin1"))
        self.assertEqual(request.get_header("X-api-key"),"test-key")


if __name__=="__main__":
    unittest.main()
