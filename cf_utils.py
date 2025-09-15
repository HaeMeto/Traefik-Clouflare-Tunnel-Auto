import httpx
from typing import List, Dict, Any, Optional


class CloudflareDNS:
    """
    Helper class untuk mengelola DNS di Cloudflare
    menggunakan REST API langsung (tanpa SDK).
    """

    def __init__(self, api_token: str, timeout: float = 10.0) -> None:
        """
        :param api_token:  Cloudflare API token dengan izin Zone.DNS Read/Edit
        :param timeout:    Timeout request HTTP (default 10 detik)
        """
        self.api_token = api_token
        self.timeout = timeout
        self.base_url = "https://api.cloudflare.com/client/v4"

    def get_cname_records(self, zone_id: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Mengambil daftar DNS CNAME record untuk domain tertentu.

        :param zone_id: ID zone Cloudflare
        :param domain:  Nama domain/FQDN
        :return:        List dict record CNAME (bisa kosong)
        """
        url = f"{self.base_url}/zones/{zone_id}/dns_records"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        params = {**params}

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, headers=headers, params=params)
           
            resp.raise_for_status()
            return resp.json().get("result", [])

    def create_cname_record(
        self,
        zone_id: str,
        name: str,
        content: str,
        proxied: bool = True,
        ttl: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Membuat CNAME record baru.

        :param zone_id: ID zone Cloudflare
        :param name:    Nama host (contoh: sub.domain.tld)
        :param content: Target CNAME
        :param proxied: Apakah proxy Cloudflare diaktifkan
        :param ttl:     Time To Live (detik), None = auto
        :return:        Dict hasil API
        """
        url = f"{self.base_url}/zones/{zone_id}/dns_records"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        data = {
            "type": "CNAME",
            "name": name,
            "content": content,
            "proxied": proxied,
        }
        if ttl:
            data["ttl"] = ttl

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=data)
            resp.raise_for_status()
            return resp.json().get("result", {})
