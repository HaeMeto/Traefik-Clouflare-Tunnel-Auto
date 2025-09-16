import os
import time
import logging
import random
from typing import List, Dict, Optional
from dataclasses import dataclass
from contextlib import contextmanager
from dotenv import load_dotenv
import requests



# Cloudflare Python SDK resmi
from cloudflare import Cloudflare

import httpx
from typing import Any

# =========================
# Helpers
# =========================

def extract_host(url: str) -> str:
    """Extract hostname/IP from URL without schema and port"""
    if not url:
        return url
    if '://' in url:
        url = url.split('://', 1)[-1]
    if '/' in url:
        url = url.split('/', 1)[0]
    if ':' in url:
        url = url.split(':', 1)[0]
    return url


# =========================
# Bootstrap
# =========================

# Load environment variables from .env file
load_dotenv(override=True)
for k in ("CLOUDFLARE_EMAIL", "CLOUDFLARE_API_KEY", "CF_EMAIL", "CF_API_KEY"):
    os.environ.pop(k, None)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# =========================
# Config
# =========================

@dataclass
class Config:
    """Konfigurasi aplikasi dari environment variables"""
    cloudflare_token: str
    cloudflare_tunnel_id: str
    cloudflare_account_id: Optional[str]
    traefik_api_endpoint: str
    traefik_entrypoints: List[str]
    traefik_service_endpoint: str
    skip_tls_routes: bool = True
    poll_interval: int = 10

    @classmethod
    def from_env(cls) -> 'Config':
        """Load konfigurasi dari environment variables dan .env file"""
        if os.path.exists('.env'):
            logger.info("Loading configuration from .env file")
        else:
            logger.warning("No .env file found, using system environment variables")

        # Parse traefik entrypoints
        entrypoints_str = os.getenv('TRAEFIK_ENTRYPOINTS')
        if entrypoints_str:
            entrypoints = [ep.strip() for ep in entrypoints_str.split(',') if ep.strip()]
        else:
            single_ep = os.getenv('TRAEFIK_ENTRYPOINT')
            if single_ep and single_ep.strip():
                entrypoints = [single_ep.strip()]
            else:
                entrypoints = []

        # Parse skip TLS routes
        skip_tls = os.getenv('SKIP_TLS_ROUTES', 'true').lower()
        if skip_tls not in ['true', 'false']:
            logger.warning(f"Invalid SKIP_TLS_ROUTES value: {skip_tls}. Using default: true")
            skip_tls = 'true'
        skip_tls_routes = skip_tls != 'false'

        # Parse poll interval
        try:
            poll_interval = int(os.getenv('POLL_INTERVAL', '10'))
            if poll_interval < 1:
                logger.warning("POLL_INTERVAL too low, setting to minimum of 1 second")
                poll_interval = 1
            elif poll_interval > 3600:
                logger.warning("POLL_INTERVAL too high, setting to maximum of 3600 seconds")
                poll_interval = 3600
        except ValueError:
            logger.warning("Invalid POLL_INTERVAL, using default 10s")
            poll_interval = 10

        config = cls(
            cloudflare_token=os.getenv('CLOUDFLARE_API_TOKEN', '').strip(),
            cloudflare_tunnel_id=os.getenv('CLOUDFLARE_TUNNEL_ID', '').strip(),
            cloudflare_account_id=os.getenv('CLOUDFLARE_ACCOUNT_ID', '').strip() or None,
            traefik_api_endpoint=os.getenv('TRAEFIK_API_ENDPOINT', '').strip(),
            traefik_entrypoints=entrypoints,
            traefik_service_endpoint=os.getenv('TRAEFIK_SERVICE_ENDPOINT', '').strip(),
            skip_tls_routes=skip_tls_routes,
            poll_interval=poll_interval
        )

        missing = []
        if not config.cloudflare_token:
            missing.append('CLOUDFLARE_API_TOKEN')
        if not config.cloudflare_tunnel_id:
            missing.append('CLOUDFLARE_TUNNEL_ID')
        if not config.traefik_api_endpoint:
            missing.append('TRAEFIK_API_ENDPOINT')
        if not config.traefik_entrypoints:
            missing.append('TRAEFIK_ENTRYPOINTS or TRAEFIK_ENTRYPOINT')
        if not config.traefik_service_endpoint:
            missing.append('TRAEFIK_SERVICE_ENDPOINT')

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        logger.info("Configuration loaded successfully:")
        logger.info(f"- Traefik API: {config.traefik_api_endpoint}")
        logger.info(f"- Entrypoints: {config.traefik_entrypoints}")
        logger.info(f"- Service Endpoint: {config.traefik_service_endpoint}")
        logger.info(f"- Skip TLS Routes: {config.skip_tls_routes}")
        logger.info(f"- Poll Interval: {config.poll_interval}s")

        return config


# =========================
# Traefik Client
# =========================

class TraefikClient:
    """Client untuk komunikasi dengan Traefik API"""
    def __init__(self, api_endpoint: str):
        self.api_endpoint = api_endpoint.rstrip('/')
        self.session = requests.Session()

    def get_routers(self) -> List[Dict]:
        """Ambil daftar router dari Traefik API"""
        try:
            resp = self.session.get(
                f"{self.api_endpoint}/api/http/routers",
                timeout=10
            )
            resp.raise_for_status()
            routers = resp.json()

            filtered_routers = []
            for router in routers:
                entrypoints = router.get('entryPoints', [])
                if any(ep != "traefik" for ep in entrypoints):
                    filtered_routers.append(router)
                    logger.debug(f"Router {router.get('name')} with entrypoints {entrypoints} included")
                else:
                    logger.debug(f"Router {router.get('name')} with entrypoints {entrypoints} excluded")

            return filtered_routers

        except requests.ConnectionError:
            logger.error(f"Connection failed to Traefik API at {self.api_endpoint}")
            logger.error("Please check if Traefik is running and accessible")
            return []

        except requests.Timeout:
            logger.error(f"Connection timeout while accessing Traefik API at {self.api_endpoint}")
            logger.error("API request took too long to respond")
            return []

        except requests.RequestException as e:
            logger.error(f"Failed to fetch routers from Traefik API: {str(e)}")
            logger.error(f"API Endpoint: {self.api_endpoint}")
            return []

        except ValueError as e:
            logger.error(f"Failed to parse JSON response from Traefik API: {str(e)}")
            return []

        except Exception as e:
            logger.error(f"Unexpected error while fetching routers: {str(e)}")
            return []


# =========================
# Retry Context
# =========================

@contextmanager
def retry_context(max_retries: int = 3):
    """Context manager untuk retry operasi dengan exponential backoff"""
    for i in range(max_retries):
        try:
            yield
            break
        except Exception as e:
            if i == max_retries - 1:
                raise
            wait_time = (2 ** i)
            logger.warning(f"Operation failed: {e}, retrying in {wait_time}s ({i+1}/{max_retries})")
            time.sleep(wait_time)


# =========================
# Cloudflare Syncer
# =========================

class CloudflareSyncer:
    """Class utama untuk sinkronisasi Traefik dengan Cloudflare"""
    def __init__(self, config: Config):
        self.config = config

        self.local_endpoint = extract_host(config.traefik_service_endpoint)
        logger.info(f"Extracted local endpoint: {self.local_endpoint}")

    
        # SDK resmi
        self.cf = Cloudflare(api_token=config.cloudflare_token)

        self.traefik = TraefikClient(config.traefik_api_endpoint)
        self._router_cache: List[Dict] = []

        # penanda domain lokal/office
        self.local_entrypoints: List[str] = []
        self.local_domains: set[str] = set()

    def get_account_id(self) -> str:
        """Ambil Cloudflare Account ID dari config atau API"""
        if self.config.cloudflare_account_id:
            return self.config.cloudflare_account_id

        logger.info("Account ID not set in config, fetching from API")
        try:
            accounts = [acc for acc in self.cf.accounts.list()]
            if not accounts:
                raise ValueError("No Cloudflare accounts found for this API token")
            account_id = accounts[0].id
            logger.info(f"Using account ID: {account_id}")
            return account_id
        except Exception as e:
            logger.error(f"Cloudflare API Error: {e}")
            raise

    def has_tls_enabled(self, router: Dict) -> bool:
        """Check if router has TLS properly configured"""
        tls = router.get('tls', {})
        return bool(
            tls.get('certResolver') and
            (tls.get('options') or tls.get('domains'))
        )

    def has_matching_entrypoint(self, router_eps: List[str]) -> bool:
        """Check if router uses any of our configured entrypoints"""
        if not self.config.traefik_entrypoints:
            return True
        return any(ep in self.config.traefik_entrypoints for ep in router_eps)

    def get_root_domain(self, domain: str) -> str:
        parts = domain.split('.')
        if len(parts) < 2:
            return domain
        return '.'.join(parts[-2:])

    def build_ingress_rules(self, routers: List[Dict]) -> List[Dict]:
        """(Opsional) Build rules dari routers - tidak dipakai di flow utama"""
        ingress = []
        processed_domains = set()

        for router in routers:
            if router.get('status') != 'enabled':
                continue
            if self.config.skip_tls_routes and self.has_tls_enabled(router):
                logger.debug(f"Skipping TLS-enabled router: {router.get('name')}")
                continue
            if not self.has_matching_entrypoint(router.get('entryPoints', [])):
                continue

            rule = router.get('rule', '')
            if not rule.startswith('Host('):
                continue

            domains = [d.strip('`') for d in rule[5:-1].split(',')]
            for domain in domains:
                if domain in processed_domains:
                    logger.info(f"Skipping duplicate domain: {domain}")
                    continue

                processed_domains.add(domain)
                logger.info(f"Adding domain to tunnel: {domain}")

                ingress.append({
                    'hostname': domain,
                    'service': self.config.traefik_service_endpoint,
                    'originRequest': {
                        'noTLSVerify': True,
                        'httpHostHeader': domain,
                        'originServerName': domain
                    }
                })

        ingress.append({'service': 'http_status:404'})
        return ingress

    def get_zones(self) -> List[Dict]:
        """Get all available zones from Cloudflare"""
        logger.info("Fetching zones from Cloudflare API")
        try:
            zones = []
            for zone in self.cf.zones.list():
                zones.append({
                    'id': zone.id,
                    'name': zone.name,
                    'account': {
                        'id': zone.account.id,
                        'name': zone.account.name
                    }
                })

            if not zones:
                logger.warning("No zones found for this API token")
                return []

            accounts = {}
            for zone in zones:
                acc_id = zone['account']['id']
                if acc_id not in accounts:
                    accounts[acc_id] = {'name': zone['account']['name'], 'zones': []}
                accounts[acc_id]['zones'].append(zone['name'])

            logger.info("--------------------------------")
            for acc_id, acc_data in accounts.items():
                logger.info(f"Account: {acc_data['name']} ({acc_id})")
                for zone_name in acc_data['zones']:
                    logger.info(f"  - {zone_name} #id: {zone_name}")
            logger.info("--------------------------------")
            return zones

        except Exception as e:
            logger.error(f"Cloudflare API Error: {e}")
            raise

    def sync_tunnel_config(self, ingress: List[Dict]) -> None:
        """(Opsional) Update tunnel configuration dengan API lama (tidak dipakai di flow utama)"""
        tunnel_id = self.config.cloudflare_tunnel_id
        zones = self.get_zones()
        if not zones:
            raise ValueError("No zones found - cannot determine account for tunnel operations")

        account_id = zones[0]['account']['id']
        logger.info(f"Using account {zones[0]['account']['name']} for tunnel operations")

        with retry_context():
            try:
                current = self.cf.zones.tunnels.configurations.get(
                    account_id=account_id,
                    tunnel_id=tunnel_id
                )
                current['config']['ingress'] = ingress
                self.cf.zones.tunnels.configurations.put(
                    account_id=account_id,
                    tunnel_id=tunnel_id,
                    data=current
                )
                logger.info("Tunnel configuration updated successfully")
            except Exception as e:
                logger.error(f"Failed to update tunnel configuration: {e}")
                raise

    def match_domains_with_zones(self, domains: List[str], zones: List[Dict]) -> Dict[str, Dict]:
        """Match domains from Traefik with Cloudflare zones"""
        domain_matches: Dict[str, Dict] = {}
        zone_map = {zone['name']: zone for zone in zones}

        for domain in domains:
            parts = domain.split('.')
            possible_domains = []
            for i in range(len(parts)-1):
                possible_domains.append('.'.join(parts[i:]))

            matched_zone = None
            matched_domain = None
            for possible_domain in possible_domains:
                if possible_domain in zone_map:
                    matched_zone = zone_map[possible_domain]
                    matched_domain = possible_domain
                    break

            if matched_zone:
                domain_matches[domain] = {
                    'zone_id': matched_zone['id'],
                    'account_id': matched_zone['account']['id'],
                    'account_name': matched_zone['account']['name'],
                    'root_domain': matched_domain
                }
                logger.info(f"Matched subdomain {domain} to zone {matched_domain} "
                            f"(Account: {matched_zone['account']['name']})")
            else:
                logger.warning(f"No matching zone found for domain: {domain}")

        return domain_matches

    def create_tunnel_config(self, domain_matches: Dict[str, Dict]) -> None:
        """Create/update Zero Trust tunnel configuration + sinkronisasi DNS"""
        logger.info("Creating or updating tunnel configuration...")
        tunnel_id = self.config.cloudflare_tunnel_id
        logger.info(f"Tunnel ID: {tunnel_id}")

        # Group domains by account
        account_domains: Dict[str, Dict[str, Any]] = {}
        for domain, match in domain_matches.items():
            acc_id = match['account_id']
            if acc_id not in account_domains:
                account_domains[acc_id] = {
                    'account_name': match['account_name'],
                    'domains': [],
                    'zone_configs': {}
                }
            account_domains[acc_id]['domains'].append(domain)
            account_domains[acc_id]['zone_configs'][domain] = match

        # Process per account
        for account_id, acc_data in account_domains.items():
            logger.info(f"Processing account: {acc_data['account_name']}")
            try:
                with retry_context():
                    # Get current tunnel config (Zero Trust path)
                    try:
                        current = self.cf.zero_trust.tunnels.cloudflared.configurations.get(
                            tunnel_id=tunnel_id,
                            account_id=account_id
                        )
                        logger.info(f"Fetched current tunnel configuration for account {acc_data['account_name']}")
                    except Exception as e:
                        logger.error(f"Failed to get tunnel configuration: {e}")
                        raise

                    # Build ingress only for non-local/offline domains
                    ingress = []
                    for domain in acc_data['domains']:
                        if domain in self.local_domains:
                            logger.info(f"Skipping tunnel ingress for local/office domain: {domain}")
                            continue

                        logger.info(f"Adding domain to tunnel: {domain}")
                        logger.info(f"Using service endpoint: {self.config.traefik_service_endpoint}")
                        ingress.append({
                            "hostname": domain,
                            "service": self.config.traefik_service_endpoint,
                            "originRequest": {
                                "noTLSVerify": True,
                                "httpHostHeader": domain,
                                "originServerName": domain
                            }
                        })

                    ingress.append({"service": "http_status:404"})

                    # Update tunnel config
                    try:
                        config_data = {"ingress": ingress}
                        self.cf.zero_trust.tunnels.cloudflared.configurations.update(
                            tunnel_id=tunnel_id,
                            account_id=account_id,
                            config=config_data
                        )
                        logger.info(f"Updated tunnel configuration for account {acc_data['account_name']}")
                    except Exception as e:
                        logger.error(f"Failed to update tunnel configuration: {e}")
                        raise

                    # ===== DNS Sync per-domain =====
                    logger.info("Syncing DNS records...")
                    for domain, match in acc_data['zone_configs'].items():
                        try:
                            # default: CNAME ke cfargotunnel, proxied=True
                            record_type = 'CNAME'
                            proxied = True
                            content = f"{tunnel_id}.cfargotunnel.com"

                            # domain lokal/office: A ke IP lokal (extracted), proxied=False
                            if domain in self.local_domains:
                                record_type = 'A'
                                proxied = False
                                content = self.local_endpoint
                                logger.info(f"[DNS] {domain} is local/office → A {content} (proxied={proxied})")

                            # cari existing record tipe yg sama
                            existing_records = list(self.cf.dns.records.list(
                                zone_id=match['zone_id'],
                                name=domain,
                                type=record_type
                            ))

                            if not existing_records:
                                logger.info(f"[DNS] No existing {record_type} record for {domain}, creating...")
                                self.cf.dns.records.create(
                                    zone_id=match['zone_id'],
                                    name=domain,
                                    type=record_type,
                                    content=content,
                                    ttl=1,
                                    proxied=proxied
                                )
                                logger.info(f"[DNS] Created {record_type} {domain} -> {content} (proxied={proxied})")
                            else:
                                rec = existing_records[0]
                                # handle object/dict dari SDK
                                rec_id = getattr(rec, 'id', None) or rec.get('id')
                                rec_content = getattr(rec, 'content', None) or rec.get('content')
                                rec_proxied = getattr(rec, 'proxied', None)
                                if rec_proxied is None:
                                    rec_proxied = rec.get('proxied')

                                needs_update = (rec_content != content) or (rec_proxied != proxied)

                                if needs_update:
                                    logger.info(f"[DNS] Updating {record_type} {domain} from {rec_content} to {content}, proxied={proxied}")
                                    self.cf.dns.records.update(
                                        zone_id=match['zone_id'],
                                        dns_record_id=rec_id,
                                        name=domain,
                                        type=record_type,
                                        content=content,
                                        ttl=1,
                                        proxied=proxied
                                    )
                                    logger.info(f"[DNS] Updated {record_type} record for {domain}")
                                else:
                                    logger.info(f"[DNS] {record_type} record for {domain} already up-to-date")
                        except Exception as e:
                            logger.error(f"Failed to manage DNS record for {domain}: {e}")

            except Exception as e:
                logger.error(f"Failed to configure tunnel for account {acc_data['account_name']}: {e}")

    def run(self):
        """Main processing loop"""
        logger.info("Starting sync loop...")
        consecutive_failures = 0
        max_failures = 3

        while True:
            try:
                # reset penanda per iterasi
                self.local_domains = set()
                self.local_entrypoints = []

                routers = self.traefik.get_routers()

                if not routers:
                    consecutive_failures += 1
                    wait_time = min(20, self.config.poll_interval * (2 ** consecutive_failures))
                    logger.warning(f"No routers found, waiting {wait_time}s before retry")
                    time.sleep(wait_time)
                    continue

                consecutive_failures = 0

                if routers == self._router_cache:
                    logger.debug("No changes in routers configuration")
                    time.sleep(self.config.poll_interval)
                    continue

                logger.info("Changes detected in Traefik routers")
                self._router_cache = routers

                # Extract domains
                domains = set()
                for router in routers:
                    logger.info(f"Router status: {router.get('status')}")
                    if router.get('status') != 'enabled':
                        logger.debug(f"Skipping disabled router: {router.get('name')}")
                        continue

                    logger.info(f"Router TLS config: {router.get('tls')}")
                    if self.config.skip_tls_routes and self.has_tls_enabled(router):
                        logger.debug(f"Skipping TLS-enabled router: {router.get('name')}")
                        # continue  # kalau ingin benar2 skip TLS, uncomment ini
                        pass

                    logger.info(f"Router entrypoints: {router.get('entryPoints', [])}")
                    if not self.has_matching_entrypoint(router.get('entryPoints', [])):
                        logger.info(f"Skipping router with non-matching entrypoints: {router.get('name')}")
                        continue

                    rule = router.get('rule', '')
                    logger.info(f"Router rule: {rule}")

                    # support format Host(`a.example.com`,`b.example.com`) dan Host(`a.example.com`)
                    import re
                    # Ambil semua isi dalam Host(`...`)
                    host_calls = re.findall(r'Host\(([^)]+)\)', rule)
                    domain_matches = []
                    for call in host_calls:
                        # pisah argumen `foo`,`bar`
                        items = [x.strip() for x in call.split(',')]
                        for item in items:
                            # ambil isi backtick `...`
                            m = re.match(r'`([^`]+)`', item)
                            if m:
                                domain_matches.append(m.group(1))

                    if domain_matches:
                        domains.update(domain_matches)

                        # Tandai domain lokal/office
                        eps = router.get('entryPoints', [])
                        if 'local' in eps or 'office' in eps:
                            logger.info(f"Router has local/office entrypoint: {router.get('name')}")
                            self.local_entrypoints.extend(domain_matches)
                            for d in domain_matches:
                                self.local_domains.add(d)
                        logger.debug(f"Added domains from router {router.get('name')}: {domain_matches}")

                domains = list(domains)

                if not domains:
                    logger.warning("No valid domains found in router rules")
                    time.sleep(self.config.poll_interval)
                    continue

                logger.info(f"Found {len(domains)} unique domains:")
                for domain in domains:
                    logger.info(f"  - {domain} {'(local/office)' if domain in self.local_domains else ''}")

                zones = self.get_zones()
                if not zones:
                    logger.warning("No Cloudflare zones found")
                    time.sleep(self.config.poll_interval)
                    continue

                domain_matches = self.match_domains_with_zones(domains, zones)
                if not domain_matches:
                    logger.warning("No domains matched with Cloudflare zones")
                    time.sleep(self.config.poll_interval)
                    continue

                # Update tunnel + DNS
                self.create_tunnel_config(domain_matches)

            except Exception as e:
                consecutive_failures += 1
                wait_time = min(20, self.config.poll_interval * (2 ** consecutive_failures))
                logger.error(f"Error in sync loop: {e}", exc_info=True)
                time.sleep(wait_time)
                continue

            jitter = random.uniform(0, self.config.poll_interval / 2)
            time.sleep(self.config.poll_interval + jitter)


# =========================
# Entrypoint
# =========================

def main():
    try:
        config = Config.from_env()
        syncer = CloudflareSyncer(config)
        syncer.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == '__main__':
    main()
