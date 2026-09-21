from common.models import Findings

def investigation_stages() -> list[tuple[str, str]]:
    return [
        ("running", "correlating auth logs (1/3)"),
        ("running", "enriching host context (2/3)"),
        ("running", "scoring risk (3/3)"),
        ("complete", "investigation complete"),
    ]

def build_findings(host: str) -> Findings:
    return Findings(
        host=host,
        summary=f"3 anomalous interactive logins detected on {host} from an unrecognized ASN",
        severity="medium",
        indicators=["185.220.101.4", "user-agent: curl/8.4", "off-hours 02:14 UTC"],
    )
