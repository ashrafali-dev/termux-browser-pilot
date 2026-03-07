"""Cookie management via CDP Network domain.

Supports get, set, delete, save/load (JSON), and import/export
in Netscape cookie format for browser interoperability.
Enables persistent sessions across browser restarts.
"""

import asyncio
import json
import os


class CookieCommands:
    """Manage browser cookies via CDP."""

    def __init__(self, session):
        self.session = session

    async def get_all(self):
        """Get all cookies from the browser."""
        result = await self.session.send("Network.getAllCookies")
        return result.get("cookies", [])

    async def get(self, urls=None):
        """Get cookies, optionally filtered by URLs."""
        params = {}
        if urls:
            params["urls"] = urls if isinstance(urls, list) else [urls]
        result = await self.session.send("Network.getCookies", params)
        return result.get("cookies", [])

    async def set(self, name, value, domain, path="/", secure=False,
                  http_only=False, same_site="Lax", expires=None):
        """Set a single cookie."""
        params = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": secure,
            "httpOnly": http_only,
            "sameSite": same_site,
        }
        if expires is not None:
            params["expires"] = expires
        await self.session.send("Network.setCookie", params)

    async def set_many(self, cookies):
        """Set multiple cookies at once.

        Args:
            cookies: list of dicts with keys: name, value, domain, etc.
        """
        await self.session.send("Network.setCookies", {"cookies": cookies})

    async def delete(self, name, domain=None, url=None):
        """Delete a specific cookie."""
        params = {"name": name}
        if domain:
            params["domain"] = domain
        if url:
            params["url"] = url
        await self.session.send("Network.deleteCookies", params)

    async def clear(self):
        """Clear all cookies."""
        await self.session.send("Network.clearBrowserCookies")

    async def save(self, filepath):
        """Save all cookies to a JSON file with restrictive permissions."""
        cookies = await self.get_all()

        def _write():
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            # Use os.open with restrictive permissions (owner read/write only)
            fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(cookies, f, indent=2)

        await asyncio.to_thread(_write)
        return len(cookies)

    async def load(self, filepath):
        """Load cookies from a JSON file into the browser."""
        def _read():
            if not os.path.exists(filepath):
                return None
            with open(filepath) as f:
                return json.load(f)

        cookies = await asyncio.to_thread(_read)
        if cookies is None:
            return 0
        if not cookies:
            return 0  # Empty list - file exists but no cookies

        # CDP setCookies needs specific fields
        clean = []
        for c in cookies:
            entry = {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            }
            if "expires" in c and c["expires"] > 0:
                entry["expires"] = c["expires"]
            if "sameSite" in c:
                entry["sameSite"] = c["sameSite"]
            clean.append(entry)
        await self.set_many(clean)
        return len(cookies)

    async def export_netscape(self, filepath):
        """Export cookies in Netscape/Mozilla cookie.txt format.

        This format is compatible with curl, wget, and browser extensions.
        """
        cookies = await self.get_all()

        def _write():
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            fd = os.open(
                filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(fd, "w") as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write("# https://curl.se/docs/http-cookies.html\n\n")
                for c in cookies:
                    domain = c.get("domain", "")
                    # Netscape format: leading dot means include subdomains
                    include_sub = "TRUE" if domain.startswith(".") else "FALSE"
                    path = c.get("path", "/")
                    secure = "TRUE" if c.get("secure") else "FALSE"
                    expires = str(int(c.get("expires", 0)))
                    name = c.get("name", "")
                    value = c.get("value", "")
                    f.write(
                        f"{domain}\t{include_sub}\t{path}\t"
                        f"{secure}\t{expires}\t{name}\t{value}\n"
                    )

        await asyncio.to_thread(_write)
        return len(cookies)

    async def import_netscape(self, filepath):
        """Import cookies from Netscape/Mozilla cookie.txt format."""
        def _read():
            if not os.path.exists(filepath):
                return None
            with open(filepath) as f:
                return f.readlines()

        lines = await asyncio.to_thread(_read)
        if lines is None:
            return 0  # File not found
        if not lines:
            return 0  # Empty file

        cookies = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain = parts[0]
            path = parts[2]
            secure = parts[3]
            expires = parts[4]
            name = parts[5]
            value = "\t".join(parts[6:])  # Value may contain tabs
            entry = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "httpOnly": False,
            }
            exp = int(expires) if expires.isdigit() else 0
            if exp > 0:
                entry["expires"] = exp
            cookies.append(entry)

        if cookies:
            await self.set_many(cookies)
        return len(cookies)

    async def print_all(self):
        """Print all cookies in a readable format."""
        cookies = await self.get_all()
        for c in cookies:
            flags = []
            if c.get("secure"):
                flags.append("Secure")
            if c.get("httpOnly"):
                flags.append("HttpOnly")
            if c.get("sameSite"):
                flags.append(f"SameSite={c['sameSite']}")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            print(f"  {c['domain']:30s} {c['name']:30s} = "
                  f"{c['value'][:40]}{flag_str}")
        return len(cookies)
