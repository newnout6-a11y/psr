import browser_cookie3
import json

def get_vk_cookies():
    try:
        cj = browser_cookie3.chrome(domain_name='vk.com')
        cookies = []
        for cookie in cj:
            cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
            })
        print(f"Found {len(cookies)} cookies for vk.com.")
        return cookies
    except Exception as e:
        print(f"Error: {e}")
        return []

if __name__ == '__main__':
    get_vk_cookies()
