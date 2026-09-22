"""
Firebase Realtime Database & Local Store Sync Manager (High-Performance Engine)
Uses Persistent HTTP Keep-Alive Connection Pooling and In-Memory Caching for Sub-Millisecond Speed.
"""

import json
import os
import time
import copy
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from cpp_engine import sort_apps_by_priority, fast_fuzzy_score

load_dotenv()

def sanitize_app_for_firebase(app):
    """
    Sanitizes app object to strictly conform to Firebase Realtime Database Security Rules.
    Rules enforce:
    - type must be one of: 'app', 'web', 'opensource', 'source'
    - $other fields must satisfy: newData.val().length <= 2000 (i.e. must be strings!)
    - featured must be boolean
    """
    clean = copy.deepcopy(app)
    
    # 1. Type validation
    valid_types = {'app', 'web', 'opensource', 'source'}
    if clean.get('type') not in valid_types:
        clean['type'] = 'app'
        
    # 2. Pinned & PinnedOrder (convert to strings because Firebase $other requires .length)
    if 'pinned' in clean:
        clean['pinned'] = 'true' if clean['pinned'] else 'false'
    if 'pinnedOrder' in clean:
        clean['pinnedOrder'] = str(clean['pinnedOrder'])
        
    # 3. Handle any non-string primitives under $other
    known_keys = {
        'id', 'name', 'type', 'tagline', 'version', 'category',
        'downloadUrl', 'webUrl', 'sourceUrl', 'icon', 'size',
        'featured', 'rating', 'downloads', 'description',
        'updatedDate', 'screenshots', 'changelog', 'versionHistory'
    }
    for k, v in list(clean.items()):
        if k not in known_keys:
            if isinstance(v, bool):
                clean[k] = 'true' if v else 'false'
            elif isinstance(v, (int, float)):
                clean[k] = str(v)
            elif v is None:
                del clean[k]
                
    return clean

def normalize_app_from_firebase(app):
    """
    Normalizes app object read from Firebase back to standard Python types.
    """
    clean = dict(app)
    if 'pinned' in clean:
        clean['pinned'] = (clean['pinned'] in (True, 'true', 'True', '1', 1))
    else:
        clean['pinned'] = False

    if 'pinnedOrder' in clean:
        try:
            clean['pinnedOrder'] = int(clean['pinnedOrder'])
        except (ValueError, TypeError):
            clean.pop('pinnedOrder', None)

    if 'featured' in clean:
        clean['featured'] = bool(clean['featured'])
        
    return clean

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

def get_apps_json_paths():
    paths = []
    p1 = os.path.join(BASE_DIR, "apps.json")
    p2 = os.path.join(ROOT_DIR, "apps.json")
    p3 = os.path.join(os.getcwd(), "apps.json")
    for p in (p1, p2, p3):
        if p not in paths:
            paths.append(p)
    return paths

def get_existing_apps_json_path():
    for p in get_apps_json_paths():
        if os.path.exists(p) and os.path.getsize(p) > 10:
            return p
    return os.path.join(BASE_DIR, "apps.json")

FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "https://mysore-a9265-default-rtdb.firebaseio.com").rstrip("/")


class FirebaseManager:
    def __init__(self):
        self.db_url = FIREBASE_DATABASE_URL
        
        # High-Speed Connection Pool Session
        self.session = requests.Session()
        retries = Retry(total=2, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=retries)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

        # In-Memory Cache for Sub-Millisecond Instant Reads
        self._cache = None
        self._cache_time = 0
        self._cache_ttl = 15  # 15 seconds cache; invalidated immediately on any write

    def _invalidate_cache(self):
        """Immediately clear cache on data writes"""
        self._cache = None
        self._cache_time = 0

    def _get_url(self, path=""):
        clean_path = path.strip("/")
        return f"{self.db_url}/{clean_path}.json" if clean_path else f"{self.db_url}/.json"

    def read_local_json(self):
        """Read fallback data from local apps.json"""
        path = get_existing_apps_json_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[Local Sync] Error reading {path}: {e}")
        return {"developer": {}, "categories": ["All", "Apps", "Web", "Open Source", "⭐ Favorites"], "apps": []}

    def write_local_json(self, data):
        """Sync updated data back to all local apps.json locations"""
        success = False
        for path in get_apps_json_paths():
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                success = True
            except Exception as e:
                print(f"[Local Sync] Error writing {path}: {e}")
        return success

    def get_full_store_data(self, force_refresh=False):
        """Fetch full store data with in-memory caching for zero latency"""
        now = time.time()
        if not force_refresh and self._cache is not None and (now - self._cache_time < self._cache_ttl):
            return self._cache

        try:
            r_apps = self.session.get(self._get_url("apps"), timeout=15)
            r_cats = self.session.get(self._get_url("categories"), timeout=15)
            r_dev = self.session.get(self._get_url("developer"), timeout=15)

            local_fallback = self.read_local_json()
            apps_data = r_apps.json() if r_apps.status_code == 200 and r_apps.json() is not None else local_fallback.get("apps", [])
            cats_data = r_cats.json() if r_cats.status_code == 200 and r_cats.json() is not None else local_fallback.get("categories", [])
            dev_data = r_dev.json() if r_dev.status_code == 200 and r_dev.json() is not None else local_fallback.get("developer", {})

            # Normalize apps array/dict
            if isinstance(apps_data, dict):
                apps_data = [dict(id=k, **v) if isinstance(v, dict) else v for k, v in apps_data.items()]
            apps_list = [normalize_app_from_firebase(a) for a in apps_data if a]

            if isinstance(cats_data, dict):
                cats_data = list(cats_data.values())

            full_data = {
                "developer": dev_data or local_fallback.get("developer", {}),
                "categories": cats_data or local_fallback.get("categories", ["All", "Apps", "Web", "Open Source", "⭐ Favorites"]),
                "apps": apps_list
            }

            self._cache = full_data
            self._cache_time = now
            return full_data
        except Exception as e:
            print(f"[Firebase] Fetch error, using local fallback: {e}")
        
        fallback = self.read_local_json()
        fallback_apps = [normalize_app_from_firebase(a) for a in fallback.get("apps", [])]
        fallback["apps"] = fallback_apps
        self._cache = fallback
        self._cache_time = now
        return fallback

    def sync_to_cloud(self, full_data, sync_extra=True):
        """Write store data to Firebase Realtime Database child nodes & local apps.json"""
        self.last_sync_error = None
        # 1. Update in-memory cache and local file
        self._cache = full_data
        self._cache_time = time.time()
        self.write_local_json(full_data)

        # 2. Update Firebase Realtime Database with strict schema sanitization
        try:
            apps = full_data.get("apps", [])
            categories = full_data.get("categories", [])
            developer = full_data.get("developer", {})

            # Strict rule compliance: sanitize fields so $other satisfies .length
            apps_payload = [sanitize_app_for_firebase(a) for a in apps]

            # Primary: Sync apps node with 25s timeout
            r_apps = self.session.put(self._get_url("apps"), json=apps_payload, timeout=25)
            apps_ok = (r_apps.status_code in (200, 201))

            if not apps_ok:
                err_text = f"HTTP {r_apps.status_code}: {r_apps.text[:100]}"
                print(f"[Firebase] Cloud sync apps error: {err_text}")
                self.last_sync_error = err_text
                return False

            # Secondary: Sync categories and developer if present (non-fatal for app update success)
            if sync_extra:
                if categories:
                    try:
                        self.session.put(self._get_url("categories"), json=categories, timeout=15)
                    except Exception as cat_e:
                        print(f"[Firebase] Non-fatal categories sync notice: {cat_e}")
                if developer:
                    try:
                        self.session.put(self._get_url("developer"), json=developer, timeout=15)
                    except Exception as dev_e:
                        print(f"[Firebase] Non-fatal developer sync notice: {dev_e}")

            return True
        except Exception as e:
            print(f"[Firebase] Cloud sync error: {e}")
            self.last_sync_error = str(e)
            return False

    def get_all_apps(self):
        """Get list of all applications sorted by C++ priority engine (Pinned first)"""
        data = self.get_full_store_data()
        apps = data.get("apps", [])
        return sort_apps_by_priority(apps)

    def get_pinned_apps(self):
        """Get only pinned/top apps sorted by rank order"""
        apps = self.get_all_apps()
        return [a for a in apps if a.get("pinned")]

    def pin_app_to_top(self, app_id):
        """Pin an app to the top (Priority #1) and adjust existing ranks"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()

        # Shift existing pinned apps down
        for a in apps:
            if a.get("pinned") and str(a.get("id", "")).lower() != clean_id:
                a["pinnedOrder"] = int(a.get("pinnedOrder", 1)) + 1

        for a in apps:
            if str(a.get("id", "")).lower() == clean_id:
                a["pinned"] = True
                a["pinnedOrder"] = 1
                a["updatedDate"] = time.strftime("%Y-%m-%d")

        data["apps"] = apps
        return self.sync_to_cloud(data)

    def unpin_app(self, app_id):
        """Remove app from pinned/top list"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()

        for a in apps:
            if str(a.get("id", "")).lower() == clean_id:
                a["pinned"] = False
                a.pop("pinnedOrder", None)
                a["updatedDate"] = time.strftime("%Y-%m-%d")

        # Re-index remaining pinned apps
        pinned_list = sorted([a for a in apps if a.get("pinned")], key=lambda x: x.get("pinnedOrder", 999))
        for idx, a in enumerate(pinned_list):
            a["pinnedOrder"] = idx + 1

        data["apps"] = apps
        return self.sync_to_cloud(data)

    def move_pinned_app(self, app_id, direction="up"):
        """Move pinned app rank up or down"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()

        pinned_list = sorted([a for a in apps if a.get("pinned")], key=lambda x: x.get("pinnedOrder", 999))
        curr_idx = next((i for i, a in enumerate(pinned_list) if str(a.get("id", "")).lower() == clean_id), -1)

        if curr_idx == -1:
            return False

        target_idx = curr_idx - 1 if direction == "up" else curr_idx + 1
        if 0 <= target_idx < len(pinned_list):
            pinned_list[curr_idx]["pinnedOrder"], pinned_list[target_idx]["pinnedOrder"] = (
                pinned_list[target_idx]["pinnedOrder"],
                pinned_list[curr_idx]["pinnedOrder"]
            )
            data["apps"] = apps
            return self.sync_to_cloud(data)
        return False

    def get_app(self, app_id):
        """Get single application by ID"""
        apps = self.get_all_apps()
        clean_id = str(app_id).strip().lower()
        for app in apps:
            if str(app.get("id", "")).lower() == clean_id:
                return app
        return None

    def add_or_update_app(self, app_dict):
        """Add new app or update existing app in Firebase and apps.json"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        app_id = str(app_dict.get("id", "")).strip()

        if not app_id:
            import uuid
            app_id = uuid.uuid4().hex[:8]
            app_dict["id"] = app_id

        existing_idx = next((i for i, a in enumerate(apps) if str(a.get("id", "")).lower() == app_id.lower()), -1)
        if existing_idx >= 0:
            apps[existing_idx] = {**apps[existing_idx], **app_dict}
        else:
            apps.append(app_dict)

        data["apps"] = apps
        return self.sync_to_cloud(data), app_id

    def delete_app(self, app_id):
        """Delete app from Firebase and apps.json"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()
        
        filtered = [a for a in apps if str(a.get("id", "")).lower() != clean_id]
        if len(filtered) != len(apps):
            data["apps"] = filtered
            return self.sync_to_cloud(data)
        return False

    def update_app_field(self, app_id, field_name, value):
        """Update a specific field of an app in real-time"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()

        for app in apps:
            if str(app.get("id", "")).lower() == clean_id:
                app[field_name] = value
                app["updatedDate"] = time.strftime("%Y-%m-%d")
                data["apps"] = apps
                return self.sync_to_cloud(data)
        return False

    def add_screenshots(self, app_id, urls_list):
        """Add one or more screenshot URLs to an app"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()

        for app in apps:
            if str(app.get("id", "")).lower() == clean_id:
                current_shots = app.get("screenshots", [])
                if not isinstance(current_shots, list):
                    current_shots = [current_shots] if current_shots else []
                for u in urls_list:
                    u_clean = u.strip()
                    if u_clean and u_clean not in current_shots:
                        current_shots.append(u_clean)
                app["screenshots"] = current_shots
                app["updatedDate"] = time.strftime("%Y-%m-%d")
                data["apps"] = apps
                return self.sync_to_cloud(data)
        return False

    def add_screenshot(self, app_id, url):
        """Add a single screenshot URL (or list) to an app"""
        if isinstance(url, (list, tuple)):
            return self.add_screenshots(app_id, list(url))
        return self.add_screenshots(app_id, [url])

    def remove_screenshot(self, app_id, index):
        """Remove a screenshot by index"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()

        for app in apps:
            if str(app.get("id", "")).lower() == clean_id:
                current_shots = app.get("screenshots", [])
                if isinstance(current_shots, list) and 0 <= index < len(current_shots):
                    current_shots.pop(index)
                    app["screenshots"] = current_shots
                    app["updatedDate"] = time.strftime("%Y-%m-%d")
                    data["apps"] = apps
                    return self.sync_to_cloud(data)
        return False

    def clear_screenshots(self, app_id):
        """Clear all screenshots for an app"""
        return self.update_app_field(app_id, "screenshots", [])

    def set_featured_app(self, app_id):
        """Mark one app as featured and others as unfeatured"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()

        for app in apps:
            if str(app.get("id", "")).lower() == clean_id:
                app["featured"] = True
            else:
                app["featured"] = False

        data["apps"] = apps
        return self.sync_to_cloud(data)

    def set_app_badge(self, app_id, badge_text, duration_days=None):
        """Set a badge/tag on an app with optional expiration time limit"""
        data = self.get_full_store_data(force_refresh=True)
        apps = data.get("apps", [])
        clean_id = str(app_id).strip().lower()

        expires_at = None
        if duration_days and duration_days > 0:
            import datetime
            exp_date = datetime.datetime.utcnow() + datetime.timedelta(days=duration_days)
            expires_at = exp_date.isoformat() + "Z"

        for app in apps:
            if str(app.get("id", "")).lower() == clean_id:
                if badge_text:
                    app["badge"] = badge_text
                    app["badgeExpiresAt"] = expires_at
                else:
                    app.pop("badge", None)
                    app.pop("badgeExpiresAt", None)
                app["updatedDate"] = time.strftime("%Y-%m-%d")
                data["apps"] = apps
                return self.sync_to_cloud(data)
        return False

    def get_developer_profile(self):
        """Get developer information"""
        data = self.get_full_store_data()
        return data.get("developer", {})

    def update_developer_profile(self, dev_dict):
        """Update developer profile"""
        data = self.get_full_store_data(force_refresh=True)
        current_dev = data.get("developer", {})
        data["developer"] = {**current_dev, **dev_dict}
        return self.sync_to_cloud(data)

    def get_categories(self, force_refresh=True):
        """Get complete category list dynamically synced from Firebase and app data"""
        data = self.get_full_store_data(force_refresh=force_refresh)
        cats_raw = data.get("categories", ["All", "Apps", "Web", "Open Source", "⭐ Favorites"])
        if isinstance(cats_raw, dict):
            cats_raw = list(cats_raw.values())

        seen = set()
        cleaned = []
        for c in cats_raw:
            if c and isinstance(c, str) and c.strip() and c.strip() not in seen:
                seen.add(c.strip())
                cleaned.append(c.strip())

        # Auto-discover any category currently used by apps
        for a in data.get("apps", []):
            cat = a.get("category")
            if cat and isinstance(cat, str) and cat.strip() and cat.strip() not in seen:
                seen.add(cat.strip())
                if "⭐ Favorites" in cleaned:
                    idx = cleaned.index("⭐ Favorites")
                    cleaned.insert(idx, cat.strip())
                else:
                    cleaned.append(cat.strip())

        # Ensure All is first and Favorites is last
        if "All" not in cleaned:
            cleaned.insert(0, "All")
        else:
            cleaned.remove("All")
            cleaned.insert(0, "All")

        if "⭐ Favorites" in cleaned:
            cleaned.remove("⭐ Favorites")
            cleaned.append("⭐ Favorites")

        return cleaned

    def add_category(self, name):
        """Add a new category chip"""
        name = name.strip()
        if not name:
            return False, "Category name cannot be empty."

        data = self.get_full_store_data(force_refresh=True)
        cats = data.get("categories", ["All", "Apps", "Web", "Open Source", "⭐ Favorites"])

        if any(c.lower() == name.lower() for c in cats):
            return False, f"Category '{name}' already exists."

        if "⭐ Favorites" in cats:
            idx = cats.index("⭐ Favorites")
            cats.insert(idx, name)
        else:
            cats.append(name)

        data["categories"] = cats
        ok = self.sync_to_cloud(data)
        return ok, f"Category '{name}' added successfully!"

    def rename_category(self, old_name, new_name):
        """Rename an existing category and update all apps belonging to it"""
        old_name = old_name.strip()
        new_name = new_name.strip()

        if old_name in ("All", "⭐ Favorites"):
            return False, f"System category '{old_name}' cannot be renamed."

        data = self.get_full_store_data(force_refresh=True)
        cats = data.get("categories", [])

        if old_name not in cats:
            return False, f"Category '{old_name}' not found."

        idx = cats.index(old_name)
        cats[idx] = new_name
        data["categories"] = cats

        for a in data.get("apps", []):
            if a.get("category") == old_name:
                a["category"] = new_name

        ok = self.sync_to_cloud(data)
        return ok, f"Renamed '{old_name}' to '{new_name}'!"

    def delete_category(self, name):
        """Delete a category and reassign apps to 'Apps'"""
        name = name.strip()
        if name in ("All", "⭐ Favorites"):
            return False, f"System category '{name}' cannot be deleted."

        data = self.get_full_store_data(force_refresh=True)
        cats = data.get("categories", [])

        if name not in cats:
            return False, f"Category '{name}' not found."

        cats.remove(name)
        data["categories"] = cats

        for a in data.get("apps", []):
            if a.get("category") == name:
                a["category"] = "Apps"

        ok = self.sync_to_cloud(data)
        return ok, f"Category '{name}' deleted and apps reassigned to 'Apps'!"

    def move_category(self, name, direction="up"):
        """Reorder category left/up or right/down"""
        name = name.strip()
        if name in ("All", "⭐ Favorites"):
            return False, "System categories cannot be moved."

        data = self.get_full_store_data(force_refresh=True)
        cats = data.get("categories", [])

        if name not in cats:
            return False, "Category not found."

        curr_idx = cats.index(name)
        target_idx = curr_idx - 1 if direction == "up" else curr_idx + 1

        if 1 <= target_idx < (len(cats) - 1 if "⭐ Favorites" in cats else len(cats)):
            cats[curr_idx], cats[target_idx] = cats[target_idx], cats[curr_idx]
            data["categories"] = cats
            return self.sync_to_cloud(data), f"Moved '{name}'!"
        return False, "Cannot move further."

    def get_analytics_stats(self):
        """Fetch download counts and stats from Firebase with connection pool"""
        try:
            res = self.session.get(self._get_url("stats/downloads"), timeout=3)
            if res.status_code == 200 and res.json():
                return res.json()
        except Exception:
            pass
        return {}


# Global Singleton Instance
firebase_mgr = FirebaseManager()
