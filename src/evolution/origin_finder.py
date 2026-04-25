"""
Модуль для обнаружения реальных IP-адресов Origin-серверов.
Использует Shodan, Censys, исторические DNS для обхода WAF.
"""

import os
import mmh3
from typing import Optional, List
from loguru import logger


class OriginFinder:
    """
    Обнаружение Origin IP за Cloudflare/DataDome.
    Методы: SSL-сертификаты, favicon hashing, исторические DNS.
    """
    
    def __init__(self, shodan_api_key: Optional[str] = None, censys_api_key: Optional[str] = None):
        self.shodan_api_key = shodan_api_key
        self.censys_api_key = censys_api_key
        self._shodan_api = None
    
    def find_by_ssl_cert(self, domain: str) -> List[str]:
        """
        Поиск Origin IP по SSL-сертификату через Shodan.
        Запрос: ssl.cert.subject.CN:"domain.com"
        """
        logger.info(f"Поиск Origin IP для {domain} через SSL-сертификаты...")
        
        try:
            import shodan
            if not self.shodan_api_key:
                logger.warning("Shodan API ключ не установлен. Использую публичный поиск.")
                return []
            
            api = shodan.Shodan(self.shodan_api_key)
            query = f'ssl:"{domain}"'
            results = api.search(query)
            
            ips = [match['ip_str'] for match in results.get('matches', [])]
            logger.info(f"Найдено {len(ips)} IP для {domain}")
            return list(set(ips))
        except Exception as e:
            logger.error(f"Ошибка поиска SSL: {e}")
            return []
    
    def find_by_favicon(self, domain: str, favicon_path: str = "/favicon.ico") -> List[str]:
        """
        Поиск серверов по хэшу favicon.ico (MurmurHash3).
        """
        logger.info(f"Поиск Origin IP для {domain} через favicon hashing...")
        
        try:
            from curl_cffi import requests as curl_requests
            
            # Скачиваем favicon
            url = f"https://{domain}{favicon_path}"
            response = curl_requests.get(url, impersonate="chrome_120")
            
            if response.status_code != 200:
                logger.warning(f"Не удалось скачать favicon: {url}")
                return []
            
            # Вычисляем хэш MurmurHash3
            favicon_hash = self._mm3_hash(response.content)
            logger.info(f"Хэш favicon: {favicon_hash}")
            
            # Ищем в Shodan
            if self.shodan_api_key:
                import shodan
                api = shodan.Shodan(self.shodan_api_key)
                query = f'http.favicon.hash:{favicon_hash}'
                results = api.search(query)
                
                ips = [match['ip_str'] for match in results.get('matches', [])]
                logger.info(f"Найдено {len(ips)} IP по favicon")
                return list(set(ips))
            
            return []
        except Exception as e:
            logger.error(f"Ошибка поиска favicon: {e}")
            return []
    
    def find_by_historical_dns(self, domain: str) -> List[str]:
        """
        Поиск по историческим DNS-записям (SecurityTrails, ViewDNS).
        """
        logger.info(f"Поиск Origin IP для {domain} через исторические DNS...")
        
        try:
            # SecurityTrails API
            from curl_cffi import requests as curl_requests
            
            url = f"https://api.securitytrails.com/v1/history/{domain}/dns/a"
            headers = {"APIKEY": os.getenv("SECURITYTRAILS_API_KEY", "")}
            
            response = curl_requests.get(url, headers=headers, impersonate="chrome_120")
            
            if response.status_code == 200:
                data = response.json()
                ips = []
                for record in data.get('records', []):
                    ips.extend(record.get('values', []))
                
                logger.info(f"Найдено {len(ips)} IP через исторические DNS")
                return list(set(ips))
            
            return []
        except Exception as e:
            logger.error(f"Ошибка исторических DNS: {e}")
            return []
    
    def verify_origin(self, ip: str, domain: str) -> bool:
        """
        Проверка, является ли IP реальным Origin-сервером.
        Отправляет запрос с Host: domain и проверяет ответ.
        """
        logger.debug(f"Проверка Origin IP: {ip} для {domain}")
        
        try:
            from curl_cffi import requests as curl_requests
            
            # Прямой запрос к IP с подменой Host
            url = f"https://{ip}"
            headers = {
                "Host": domain,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
            }
            
            response = curl_requests.get(
                url,
                headers=headers,
                impersonate="chrome_120",
                verify=False,  # SSL может не совпадать
                timeout=10
            )
            
            is_origin = response.status_code in [200, 301, 302]
            logger.info(f"IP {ip} → {'Origin' if is_origin else 'Не Origin'} ({response.status_code})")
            return is_origin
        except Exception as e:
            logger.warning(f"Ошибка проверки IP {ip}: {e}")
            return False
    
    def get_working_origins(self, domain: str) -> List[str]:
        """
        Получить все работающие Origin IP для домена.
        """
        all_ips = []
        all_ips.extend(self.find_by_ssl_cert(domain))
        all_ips.extend(self.find_by_favicon(domain))
        all_ips.extend(self.find_by_historical_dns(domain))
        
        # Фильтруем рабочие
        working = [ip for ip in set(all_ips) if self.verify_origin(ip, domain)]
        logger.info(f"Рабочие Origin IP для {domain}: {working}")
        return working
    
    def _mm3_hash(self, data: bytes) -> int:
        """Вычислить MurmurHash3 для favicon (как у Shodan)."""
        h = mmh3.hash(data)
        return h
    
    def request_with_origin(
        self,
        origin_ip: str,
        domain: str,
        path: str = "/",
        method: str = "GET",
        **kwargs
    ):
        """
        Отправить запрос напрямую к Origin IP, минуя WAF.
        """
        from curl_cffi import requests as curl_requests
        
        url = f"https://{origin_ip}{path}"
        headers = kwargs.pop("headers", {})
        headers["Host"] = domain
        
        logger.debug(f"Прямой запрос к Origin: {origin_ip} (Host: {domain})")
        
        return curl_requests.request(
            method=method,
            url=url,
            headers=headers,
            verify=False,
            impersonate="chrome_120",
            **kwargs
        )
