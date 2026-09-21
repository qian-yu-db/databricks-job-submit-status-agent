from agent.investigation import query_suspicious_logins, build_findings_from_rows, investigate


def test_query_passes_table_and_host_param():
    seen = {}

    def fake(sql, params):
        seen["sql"] = sql
        seen["params"] = params
        return []

    query_suspicious_logins("web-prod-04", table="cat.sch.auth_events", execute_sql=fake)
    assert "cat.sch.auth_events" in seen["sql"]
    assert seen["params"] == {"host": "web-prod-04"}


def test_findings_from_anomaly_rows_high_severity():
    rows = [
        {
            "source_asn": "AS-TOR-EXIT",
            "user_id": "svc-deploy",
            "event_type": "login_failed",
            "events": "45",
            "distinct_ips": "44",
            "first_seen": "x",
            "last_seen": "y",
        },
        {
            "source_asn": "AS-TOR-EXIT",
            "user_id": "svc-deploy",
            "event_type": "login_success",
            "events": "5",
            "distinct_ips": "5",
            "first_seen": "x",
            "last_seen": "y",
        },
    ]
    f = build_findings_from_rows("web-prod-04", rows)
    assert f.severity == "high"
    assert "AS-TOR-EXIT" in f.indicators and "user:svc-deploy" in f.indicators
    assert "web-prod-04" in f.summary


def test_findings_from_empty_rows_low():
    f = build_findings_from_rows("clean-host", [])
    assert f.severity == "low" and f.indicators == []


def test_investigate_falls_back_to_mock_without_executor():
    f = investigate("web-prod-04")  # no execute_sql/table -> mock
    assert f.host == "web-prod-04"   # returns stages.build_findings result


def test_findings_counts_scoped_to_top_actor():
    rows = [
        {"source_asn": "AS-TOR-EXIT", "user_id": "svc-deploy", "event_type": "login_failed", "events": "45", "distinct_ips": "44", "first_seen": "x", "last_seen": "y"},
        {"source_asn": "AS-TOR-EXIT", "user_id": "svc-deploy", "event_type": "login_success", "events": "5", "distinct_ips": "5", "first_seen": "x", "last_seen": "y"},
        {"source_asn": "AS7922-Comcast", "user_id": "u01000", "event_type": "login_success", "events": "180", "distinct_ips": "3", "first_seen": "x", "last_seen": "y"},
    ]
    f = build_findings_from_rows("web-prod-04", rows)
    assert "failed:45" in f.indicators and "success:5" in f.indicators   # NOT 185
    assert "user:svc-deploy" in f.indicators and "AS-TOR-EXIT" in f.indicators
