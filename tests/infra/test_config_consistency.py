"""設定ファイル間の整合性テスト

docker-compose, telegraf, grafana, mosquitto, .env.example の間で
名前・ポート・環境変数が一致しているかを静的に検証する。
ランタイム不要、CI でも回せる。
"""

import re
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]  # uecs-llm/


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _load_yaml(relpath: str) -> dict:
    return yaml.safe_load(_read(relpath))


def _parse_env_example(relpath: str) -> dict[str, str]:
    """Parse .env.example into {KEY: value} dict (ignoring comments)."""
    result = {}
    for line in _read(relpath).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def _extract_env_vars(text: str) -> set[str]:
    """Extract ${VAR} and $VAR references from text."""
    # ${VAR_NAME} or ${VAR_NAME:-default}
    pattern1 = re.findall(r"\$\{([A-Z_][A-Z0-9_]*)(?::?-[^}]*)?\}", text)
    # $VAR_NAME (standalone, e.g. in telegraf.conf)
    pattern2 = re.findall(r"\$([A-Z_][A-Z0-9_]*)\b", text)
    return set(pattern1) | set(pattern2)


def _parse_telegraf_conf(text: str) -> dict:
    """Extract key settings from telegraf.conf (TOML-like)."""
    result = {}
    # InfluxDB output
    m = re.search(r'organization\s*=\s*"([^"]+)"', text)
    if m:
        result["influxdb_org"] = m.group(1)
    m = re.search(r'bucket\s*=\s*"([^"]+)"', text)
    if m:
        result["influxdb_bucket"] = m.group(1)
    # InfluxDB URL
    m = re.search(r'urls\s*=\s*\["([^"]+)"\]', text)
    if m:
        result["influxdb_url"] = m.group(1)
    # MQTT servers
    result["mqtt_servers"] = re.findall(r'servers\s*=\s*\["([^"]+)"\]', text)
    # measurement names
    result["measurements"] = re.findall(r'name_override\s*=\s*"([^"]+)"', text)
    return result


# ---------------------------------------------------------------------------
# Tests: InfluxDB bucket / org 整合性
# ---------------------------------------------------------------------------


class TestInfluxDBConsistency:
    """InfluxDB の org名・bucket名が全設定ファイルで一致しているか。"""

    def test_telegraf_bucket_matches_docker_compose(self):
        """telegraf.conf の bucket == docker-compose の INIT_BUCKET."""
        dc = _read("cloud/docker-compose.yaml")
        m = re.search(r"DOCKER_INFLUXDB_INIT_BUCKET=(\S+)", dc)
        assert m, "DOCKER_INFLUXDB_INIT_BUCKET not found in docker-compose"
        dc_bucket = m.group(1)

        telegraf = _parse_telegraf_conf(_read("cloud/telegraf/telegraf.conf"))
        assert telegraf["influxdb_bucket"] == dc_bucket, (
            f"telegraf bucket '{telegraf['influxdb_bucket']}' != "
            f"docker-compose INIT_BUCKET '{dc_bucket}'"
        )

    def test_telegraf_org_matches_docker_compose(self):
        """telegraf.conf の organization == docker-compose の INIT_ORG."""
        dc = _read("cloud/docker-compose.yaml")
        m = re.search(r"DOCKER_INFLUXDB_INIT_ORG=(\S+)", dc)
        assert m, "DOCKER_INFLUXDB_INIT_ORG not found in docker-compose"
        dc_org = m.group(1)

        telegraf = _parse_telegraf_conf(_read("cloud/telegraf/telegraf.conf"))
        assert telegraf["influxdb_org"] == dc_org, (
            f"telegraf org '{telegraf['influxdb_org']}' != "
            f"docker-compose INIT_ORG '{dc_org}'"
        )

    def test_grafana_datasource_matches_docker_compose(self):
        """Grafana datasource の org/bucket == docker-compose."""
        dc = _read("cloud/docker-compose.yaml")
        dc_org = re.search(r"DOCKER_INFLUXDB_INIT_ORG=(\S+)", dc).group(1)
        dc_bucket = re.search(r"DOCKER_INFLUXDB_INIT_BUCKET=(\S+)", dc).group(1)

        ds = _load_yaml("cloud/grafana/provisioning/datasources/influxdb.yaml")
        ds_conf = ds["datasources"][0]
        assert ds_conf["jsonData"]["organization"] == dc_org
        assert ds_conf["jsonData"]["defaultBucket"] == dc_bucket

    def test_alerting_bucket_matches_docker_compose(self):
        """alerting.yaml 内の Flux クエリの bucket == docker-compose の INIT_BUCKET."""
        dc = _read("cloud/docker-compose.yaml")
        dc_bucket = re.search(r"DOCKER_INFLUXDB_INIT_BUCKET=(\S+)", dc).group(1)

        alerting_text = _read("cloud/grafana/provisioning/alerting.yaml")
        buckets_in_queries = set(re.findall(
            r'from\(bucket:\s*"([^"]+)"\)', alerting_text
        ))
        assert buckets_in_queries, "No Flux bucket references found in alerting.yaml"
        for bucket in buckets_in_queries:
            assert bucket == dc_bucket, (
                f"alerting.yaml Flux query uses bucket '{bucket}' "
                f"but docker-compose INIT_BUCKET is '{dc_bucket}'"
            )

    def test_alerting_measurement_matches_telegraf(self):
        """alerting.yaml の measurement == telegraf.conf の name_override."""
        telegraf = _parse_telegraf_conf(_read("cloud/telegraf/telegraf.conf"))
        expected_measurements = set(telegraf["measurements"])

        alerting_text = _read("cloud/grafana/provisioning/alerting.yaml")
        measurements_in_queries = set(re.findall(
            r'r\._measurement\s*==\s*"([^"]+)"', alerting_text
        ))
        for m in measurements_in_queries:
            assert m in expected_measurements, (
                f"alerting.yaml references measurement '{m}' "
                f"but telegraf.conf only defines {expected_measurements}"
            )


# ---------------------------------------------------------------------------
# Tests: Grafana datasource UID 整合性
# ---------------------------------------------------------------------------


class TestGrafanaDatasourceUID:
    """Grafana alerting.yaml の datasourceUid が datasources/*.yaml と一致するか。"""

    def test_alerting_datasource_uid_matches(self):
        ds = _load_yaml("cloud/grafana/provisioning/datasources/influxdb.yaml")
        ds_uid = ds["datasources"][0]["uid"]

        alerting_text = _read("cloud/grafana/provisioning/alerting.yaml")
        # datasourceUid: xxx (YAML key) — exclude __expr__
        uids = set(re.findall(r"datasourceUid:\s*([a-zA-Z0-9_-]+)", alerting_text))
        uids.discard("__expr__")
        assert uids, "No datasourceUid found in alerting.yaml"
        for uid in uids:
            assert uid == ds_uid, (
                f"alerting.yaml references datasourceUid '{uid}' "
                f"but influxdb.yaml defines uid '{ds_uid}'"
            )

    def test_dashboard_datasource_uid_matches(self):
        """dashboard JSON の datasource uid が datasources/*.yaml と一致するか。"""
        import json

        ds = _load_yaml("cloud/grafana/provisioning/datasources/influxdb.yaml")
        ds_uid = ds["datasources"][0]["uid"]

        dashboard_path = ROOT / "cloud/grafana/dashboards/greenhouse_weather.json"
        if not dashboard_path.exists():
            return  # optional
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))

        # Dashboard uses ${DS_INFLUXDB} template variable — that's fine
        # But check for hardcoded UIDs
        text = dashboard_path.read_text(encoding="utf-8")
        hardcoded_uids = set(re.findall(r'"uid":\s*"([^$][^"]*)"', text))
        # Filter out common non-datasource uids
        hardcoded_uids -= {"greenhouse_weather", "line-webhook", "-- Dashboard --"}
        for uid in hardcoded_uids:
            if uid.startswith("influx") or uid.startswith("agriha"):
                assert uid == ds_uid, (
                    f"Dashboard has hardcoded datasource uid '{uid}' "
                    f"but influxdb.yaml defines '{ds_uid}'"
                )


    def test_dashboard_bucket_matches_docker_compose(self):
        """dashboard JSON の Flux クエリの bucket == docker-compose の INIT_BUCKET."""
        import json

        dc = _read("cloud/docker-compose.yaml")
        dc_bucket = re.search(r"DOCKER_INFLUXDB_INIT_BUCKET=(\S+)", dc).group(1)

        dashboard_path = ROOT / "cloud/grafana/dashboards/greenhouse_weather.json"
        if not dashboard_path.exists():
            return
        text = dashboard_path.read_text(encoding="utf-8")
        buckets = set(re.findall(r'from\(bucket:\s*\\?"([^"\\]+)\\?"', text))
        for bucket in buckets:
            assert bucket == dc_bucket, (
                f"Dashboard Flux query uses bucket '{bucket}' "
                f"but docker-compose INIT_BUCKET is '{dc_bucket}'"
            )

    def test_dashboard_measurements_match_telegraf(self):
        """dashboard JSON の measurement == telegraf.conf の name_override."""
        import json

        telegraf = _parse_telegraf_conf(_read("cloud/telegraf/telegraf.conf"))
        expected = set(telegraf["measurements"])

        dashboard_path = ROOT / "cloud/grafana/dashboards/greenhouse_weather.json"
        if not dashboard_path.exists():
            return
        text = dashboard_path.read_text(encoding="utf-8")
        measurements = set(re.findall(r'r\._measurement\s*==\s*\\?"([^"\\]+)\\?"', text))
        for m in measurements:
            assert m in expected, (
                f"Dashboard references measurement '{m}' "
                f"but telegraf.conf only defines {expected}"
            )


# ---------------------------------------------------------------------------
# Tests: 環境変数整合性
# ---------------------------------------------------------------------------


class TestEnvVarConsistency:
    """設定ファイルが参照する環境変数が .env.example に定義されているか。"""

    def test_docker_compose_env_vars_in_env_example(self):
        """cloud/docker-compose.yaml の ${VAR} が .env.example にあるか。"""
        dc_text = _read("cloud/docker-compose.yaml")
        env_keys = _parse_env_example("cloud/.env.example")

        refs = _extract_env_vars(dc_text)
        # TZ は docker 標準、除外
        refs.discard("TZ")
        for var in refs:
            assert var in env_keys, (
                f"docker-compose.yaml references ${{{var}}} "
                f"but it's not in .env.example"
            )

    def test_alerting_env_vars_in_env_example(self):
        """alerting.yaml の ${VAR} が .env.example にあるか。"""
        alerting_text = _read("cloud/grafana/provisioning/alerting.yaml")
        env_keys = _parse_env_example("cloud/.env.example")

        refs = _extract_env_vars(alerting_text)
        for var in refs:
            assert var in env_keys, (
                f"alerting.yaml references ${{{var}}} "
                f"but it's not in .env.example"
            )

    def test_grafana_datasource_env_vars_in_env_example(self):
        """datasources/influxdb.yaml の ${VAR} が .env.example にあるか。"""
        ds_text = _read("cloud/grafana/provisioning/datasources/influxdb.yaml")
        env_keys = _parse_env_example("cloud/.env.example")

        refs = _extract_env_vars(ds_text)
        for var in refs:
            assert var in env_keys, (
                f"datasources/influxdb.yaml references ${{{var}}} "
                f"but it's not in .env.example"
            )


# ---------------------------------------------------------------------------
# Tests: Mosquitto 設定
# ---------------------------------------------------------------------------


class TestMosquittoConfig:
    """Mosquitto が外部接続を受け付ける設定になっているか。"""

    def test_listener_binds_all_interfaces(self):
        """listener が 0.0.0.0 にバインドされているか（VPN経由の接続受付に必要）。"""
        conf = _read("docker/mosquitto/mosquitto.conf")
        # "listener 1883 0.0.0.0" のような行があるか
        assert re.search(r"^listener\s+1883\s+0\.0\.0\.0", conf, re.MULTILINE), (
            "Mosquitto listener 1883 is not bound to 0.0.0.0 — "
            "external connections (e.g. from nuc via VPN) will be refused"
        )

    def test_allow_anonymous_is_set(self):
        """開発環境で allow_anonymous true が設定されているか。"""
        conf = _read("docker/mosquitto/mosquitto.conf")
        assert re.search(r"^allow_anonymous\s+true", conf, re.MULTILINE), (
            "allow_anonymous is not true — connections without auth will be refused"
        )

    def test_mqtt_port_matches_docker_compose(self):
        """mosquitto.conf の listener ポートが docker-compose のポートマッピングと一致。"""
        conf = _read("docker/mosquitto/mosquitto.conf")
        listener_ports = set(re.findall(r"^listener\s+(\d+)", conf, re.MULTILINE))

        dc = _load_yaml("docker/docker-compose.yaml")
        mqtt_service = dc["services"]["mosquitto"]
        dc_ports = set()
        for p in mqtt_service.get("ports", []):
            # "1883:1883" → 1883
            host_port = str(p).split(":")[0].strip('"')
            dc_ports.add(host_port)

        for port in listener_ports:
            assert port in dc_ports, (
                f"mosquitto.conf listens on port {port} "
                f"but docker-compose only exposes {dc_ports}"
            )


# ---------------------------------------------------------------------------
# Tests: Telegraf → InfluxDB 接続
# ---------------------------------------------------------------------------


class TestTelegrafConnection:
    """Telegraf が正しい InfluxDB コンテナ名を参照しているか。"""

    def test_telegraf_influxdb_url_uses_container_name(self):
        """telegraf.conf の InfluxDB URL がコンテナ名 'influxdb' を使っているか。"""
        telegraf = _parse_telegraf_conf(_read("cloud/telegraf/telegraf.conf"))
        url = telegraf.get("influxdb_url", "")
        assert "influxdb" in url, (
            f"telegraf.conf InfluxDB URL is '{url}' — "
            f"expected 'http://influxdb:8086' (Docker service name)"
        )

    def test_telegraf_mqtt_uses_env_var(self):
        """telegraf.conf の MQTT URL が環境変数を使っているか。"""
        telegraf = _parse_telegraf_conf(_read("cloud/telegraf/telegraf.conf"))
        for server in telegraf.get("mqtt_servers", []):
            assert "MQTT_BROKER_HOST" in server, (
                f"telegraf MQTT server '{server}' does not use "
                f"$MQTT_BROKER_HOST env var — will break in different envs"
            )


# ---------------------------------------------------------------------------
# Tests: ファイル存在チェック
# ---------------------------------------------------------------------------


class TestFileExistence:
    """docker-compose 等が参照するファイルが実在するか。"""

    def test_cloud_telegraf_conf_exists(self):
        assert (ROOT / "cloud/telegraf/telegraf.conf").exists()

    def test_cloud_grafana_provisioning_exists(self):
        assert (ROOT / "cloud/grafana/provisioning/datasources/influxdb.yaml").exists()
        assert (ROOT / "cloud/grafana/provisioning/dashboards/dashboards.yaml").exists()
        assert (ROOT / "cloud/grafana/provisioning/alerting.yaml").exists()

    def test_cloud_grafana_dashboards_exist(self):
        assert (ROOT / "cloud/grafana/dashboards/greenhouse_weather.json").exists()

    def test_docker_mosquitto_conf_exists(self):
        assert (ROOT / "docker/mosquitto/mosquitto.conf").exists()

    def test_linebot_dockerfile_exists(self):
        """cloud/docker-compose.yaml の build: ../linebot が有効か。"""
        assert (ROOT / "linebot/Dockerfile").exists()

    def test_env_examples_exist(self):
        assert (ROOT / "cloud/.env.example").exists()
        assert (ROOT / "linebot/.env.example").exists()

    def test_camera_upload_script_exists(self):
        """RPi カメラアップロードスクリプトが存在するか。"""
        assert (ROOT / "image/camera_upload.sh").exists()

    def test_gitignore_excludes_env(self):
        gitignore = _read(".gitignore")
        assert ".env" in gitignore, ".gitignore does not exclude .env files"
