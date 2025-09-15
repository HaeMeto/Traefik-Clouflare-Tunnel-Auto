import os
import time
import logging
import random
from typing import List, Dict, Optional
from dataclasses import dataclass
from contextlib import contextmanager
from dotenv import load_dotenv
import requests
from cf_utils import CloudflareDNS   
# --- imports (letakkan di atas file) ---
from cloudflare import Cloudflare

import httpx
from typing import List, Dict, Any


# Load environment variables from .env file
load_dotenv(override=True)
for k in ("CLOUDFLARE_EMAIL", "CLOUDFLARE_API_KEY", "CF_EMAIL", "CF_API_KEY"):
    os.environ.pop(k, None)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
        # Check for .env file
        if os.path.exists('.env'):
            logger.info("Loading configuration from .env file")
        else:
            logger.warning("No .env file found, using system environment variables")

        # Parse traefik entrypoints with validation
        entrypoints_str = os.getenv('TRAEFIK_ENTRYPOINTS')
        if entrypoints_str:
            entrypoints = [ep.strip() for ep in entrypoints_str.split(',') if ep.strip()]
        else:
            # Backward compatibility
            single_ep = os.getenv('TRAEFIK_ENTRYPOINT')
            if single_ep and single_ep.strip():
                entrypoints = [single_ep.strip()]
            else:
                entrypoints = []

        # Parse skip TLS routes with validation
        skip_tls = os.getenv('SKIP_TLS_ROUTES', 'true').lower()
        if skip_tls not in ['true', 'false']:
            logger.warning(f"Invalid SKIP_TLS_ROUTES value: {skip_tls}. Using default: true")
            skip_tls = 'true'
        skip_tls_routes = skip_tls != 'false'

        # Parse poll interval with bounds checking
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

        # Validate required configs
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

        # Log non-sensitive configuration
        logger.info("Configuration loaded successfully:")
        logger.info(f"- Traefik API: {config.traefik_api_endpoint}")
        logger.info(f"- Entrypoints: {config.traefik_entrypoints}")
        logger.info(f"- Service Endpoint: {config.traefik_service_endpoint}")
        logger.info(f"- Skip TLS Routes: {config.skip_tls_routes}")
        logger.info(f"- Poll Interval: {config.poll_interval}s")

        return config

class TraefikClient:
    """Client untuk komunikasi dengan Traefik API"""
    def __init__(self, api_endpoint: str):
        self.api_endpoint = api_endpoint.rstrip('/')
        self.session = requests.Session()

    def get_routers(self) -> List[Dict]:
        """Ambil daftar router dari Traefik API"""
        resp = self.session.get(f"{self.api_endpoint}/api/http/routers")
        resp.raise_for_status()
        routers = resp.json()
        
        # Filter out routers that only have "traefik" entrypoint
        filtered_routers = []
        for router in routers:
            entrypoints = router.get('entryPoints', [])
            # Keep router if it has any entrypoint other than "traefik"
            if any(ep != "traefik" for ep in entrypoints):
                filtered_routers.append(router)
                logger.debug(f"Router {router.get('name')} with entrypoints {entrypoints} included")
            else:
                logger.debug(f"Router {router.get('name')} with entrypoints {entrypoints} excluded")

        # logger.info(f"Found {len(filtered_routers)} routers (excluding traefik-only entrypoints)")
        return filtered_routers

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
            wait_time = (2 ** i)  # exponential backoff
            logger.warning(f"Operation failed: {e}, retrying in {wait_time}s ({i+1}/{max_retries})")
            time.sleep(wait_time)

class CloudflareSyncer:
    """Class utama untuk sinkronisasi Traefik dengan Cloudflare"""
    def __init__(self, config: Config):
        self.config = config
        # Initialize Cloudflare client with token
        headers = {
            'Authorization': f'Bearer {config.cloudflare_token}'
        }
        
        self.cf_helpers = CloudflareDNS(api_token=config.cloudflare_token)
        

        self.cf = Cloudflare(api_token=config.cloudflare_token)
        self.traefik = TraefikClient(config.traefik_api_endpoint)
        self._router_cache = []

    def get_account_id(self) -> str:
        """Ambil Cloudflare Account ID dari config atau API"""
        if self.config.cloudflare_account_id:
            return self.config.cloudflare_account_id

        logger.info("Account ID not set in config, fetching from API")

        try:
            # Get accounts list
            accounts = []
            for account in self.cf.accounts.list():
                accounts.append(account)

            if not accounts:
                raise ValueError("No Cloudflare accounts found for this API token")

            # Access the ID attribute directly from the Account object
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
        """Extract root domain from full domain name"""
        parts = domain.split('.')
        if len(parts) < 2:
            return domain
        return '.'.join(parts[-2:])

    def build_ingress_rules(self, routers: List[Dict]) -> List[Dict]:
        """Build Cloudflare tunnel ingress rules from Traefik routers"""
        ingress = []
        processed_domains = set()

        for router in routers:
            # Skip disabled routes
            if router.get('status') != 'enabled':
                continue

            # Skip TLS routes if configured
            if self.config.skip_tls_routes and self.has_tls_enabled(router):
                logger.debug(f"Skipping TLS-enabled router: {router.get('name')}")
                continue

            # Check entrypoints
            if not self.has_matching_entrypoint(router.get('entryPoints', [])):
                continue

            # Parse domains from Host rule
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

        # Add catch-all rule
        ingress.append({
            'service': 'http_status:404'
        })

        return ingress

    def get_zones(self) -> List[Dict]:
        """Get all available zones from Cloudflare"""
        logger.info("Fetching zones from Cloudflare API")
        
        try:
            zones = []
            # Get all zones with pagination
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

            # Log found zones grouped by account
            accounts = {}
            for zone in zones:
                print(zone)
                acc_id = zone['account']['id']
                if acc_id not in accounts:
                    accounts[acc_id] = {
                        'name': zone['account']['name'],
                        'zones': []
                    }
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
        """Update tunnel configuration with new ingress rules"""
        tunnel_id = self.config.cloudflare_tunnel_id
        zones = self.get_zones()

        if not zones:
            raise ValueError("No zones found - cannot determine account for tunnel operations")

        # Use first zone's account for tunnel operations
        account_id = zones[0]['account']['id']
        logger.info(f"Using account {zones[0]['account']['name']} for tunnel operations")

        with retry_context():
            try:
                # Get current config
                current = self.cf.zones.tunnels.configurations.get(
                    account_id=account_id,
                    tunnel_id=tunnel_id
                )
                
                # Update ingress rules
                current['config']['ingress'] = ingress
                
                # Save changes
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
        domain_matches = {}
        # Create a map of root domains to zone info
        zone_map = {zone['name']: zone for zone in zones}
        
        for domain in domains:
            # Get all possible parent domains
            parts = domain.split('.')
            possible_domains = []
            for i in range(len(parts)-1):
                possible_domains.append('.'.join(parts[i:]))
            
            # Find the matching zone (if any)
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
                logger.debug(f"Tried matching against: {possible_domains}")

        return domain_matches

    def create_tunnel_config(self, domain_matches: Dict[str, Dict]) -> None:
        """Create or update Zero Trust tunnel configuration"""
        logger.info("Creating or updating tunnel configuration...")
        tunnel_id = self.config.cloudflare_tunnel_id
        tunnel_domain = f"{tunnel_id}.cfargotunnel.com"

        logger.info(f"Using tunnel domain: {tunnel_domain}")
        logger.info(f"Tunnel ID: {tunnel_id}")

        # Group domains by account
        account_domains = {}
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

        # Process each account
        for account_id, acc_data in account_domains.items():
            logger.info(f"Processing account: {acc_data['account_name']}")

            try:
                # Configure tunnel for this account
                with retry_context():
                    # Get current tunnel config using correct API path
                    try:
                        # Use argo.tunnels instead of accounts.tunnels
                        current = self.cf.zero_trust.tunnels.cloudflared.configurations.get(
                            tunnel_id=tunnel_id,
                            account_id=account_id
                        )
                    except Exception as e:
                        logger.error(f"Failed to get tunnel configuration: {e}")
                        raise

                    # Build ingress rules for domains in this account
                    ingress = []
                    for domain in acc_data['domains']:
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

                    # Add catch-all rule
                    ingress.append({"service": "http_status:404"})

                    # Update tunnel config using correct API path
                    try:
                        config_data = {
                           
                                "ingress": ingress
                            
                        }
                        logger.info(f"Config data to update: {config_data}")
                        # Use argo.tunnels for configuration update
                        self.cf.zero_trust.tunnels.cloudflared.configurations.update(
                            tunnel_id=tunnel_id,
                            account_id=account_id,
                            config=config_data
                        )
                        logger.info(f"Updated tunnel configuration for account {acc_data['account_name']}")
                    except Exception as e:
                        logger.error(f"Failed to update tunnel configuration: {e}")
                        raise

                    # Update DNS records
                    logger.info("Syncing DNS records...")
                    for domain, match in acc_data['zone_configs'].items():
                        try:
                        
                            records = self.cf_helpers.get_cname_records(zone_id=match["zone_id"],
                            params={"name": domain, "type": "CNAME"})
                        
                            if not records:
                                logger.info(f"No existing DNS record for {domain}, creating new one")
                                self.cf.dns.records.create(zone_id=match['zone_id'], name=domain, type='CNAME', content=tunnel_domain, ttl=1, proxied=True)
                                logger.info(f"Created DNS record for {domain}")
                            elif records[0]['content'] != tunnel_domain:
                                logger.info(f"Existing DNS record for {domain} points to {records[0]['content']}, updating to {tunnel_domain}")
                                self.cf.dns.records.update(
                                    dns_record_id=records[0]['id'],
                                    zone_id=match['zone_id'],
                                    name=domain, type='CNAME', content=tunnel_domain, ttl=1, proxied=True
                                )
                                logger.info(f"Updated DNS record for {domain}")
                            else:
                                logger.info(f"DNS record for {domain} is already correct, no action needed")
                                
                        except Exception as e:
                            logger.error(f"Failed to manage DNS record for {domain}: {e}")

            except Exception as e:
                logger.error(f"Failed to configure tunnel for account {acc_data['account_name']}: {e}")

    def run(self):
        """Main processing loop"""
        logger.info("Starting sync loop...")
        
        while True:
            try:
                # Get Traefik routers
                routers = self.traefik.get_routers()
                
                # print(routers)
                # Skip if no changes
                if routers == self._router_cache:
                    logger.debug("No changes in routers configuration")
                    time.sleep(self.config.poll_interval)
                    continue

                logger.info("Changes detected in Traefik routers")
                self._router_cache = routers

                # Extract domains from routers using set for uniqueness
                domains = set()
                for router in routers:
                    
                    # Skip if router is not enabled
                    logger.info(f"Router status: {router.get('status')}")
                    if router.get('status') != 'enabled':
                        logger.debug(f"Skipping disabled router: {router.get('name')}")
                        continue
                    
                    # Skip TLS routes if configured
                    logger.info(f"Router TLS config: {router.get('tls')}")
                    if self.config.skip_tls_routes and self.has_tls_enabled(router):
                        logger.debug(f"Skipping TLS-enabled router: {router.get('name')}")
                        pass

                    
                    # Check entrypoints
                    logger.info(f"Router entrypoints: {router.get('entryPoints', [])}")
                    if not self.has_matching_entrypoint(router.get('entryPoints', [])):
                        logger.debug(f"Skipping router with non-matching entrypoints: {router.get('name')}")
                        continue
                    
                    
                    # Extract domains from Host rule
                    
                    rule = router.get('rule', '')
                    logger.info(f"Router rule: {rule}")
                    if 'Host(`' in rule:
                        import re
                        domain_matches = re.findall(r'Host\(`([^`]+)`\)', rule)
                        if domain_matches:
                            
                            domains.update(domain_matches)
                            logger.debug(f"Added domains from router {router.get('name')}: {domain_matches}")

                # Convert set back to list
                print(domains)
                domains = list(domains)

                if not domains:
                    logger.warning("No valid domains found in router rules")
                    time.sleep(self.config.poll_interval)
                    continue

                # Log found domains
                logger.info(f"Found {len(domains)} unique domains:")
                for domain in domains:
                    logger.info(f"  - {domain}")

                # Get Cloudflare zones
                zones = self.get_zones()
                if not zones:
                    logger.warning("No Cloudflare zones found")
                    time.sleep(self.config.poll_interval)
                    continue

                # Match domains with zones
                domain_matches = self.match_domains_with_zones(domains, zones)
                print(domain_matches)
                if not domain_matches:
                    logger.warning("No domains matched with Cloudflare zones")
                    time.sleep(self.config.poll_interval)
                    continue

                # Update tunnel configuration
                self.create_tunnel_config(domain_matches)

            except Exception as e:
                logger.error(f"Error in sync loop: {e}", exc_info=True)
                time.sleep(self.config.poll_interval)
                continue

            # Add jitter to avoid thundering herd
            jitter = random.uniform(0, self.config.poll_interval / 2)
            time.sleep(self.config.poll_interval + jitter)

def main():
    """Entry point"""
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
