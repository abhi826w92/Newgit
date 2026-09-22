"""
GitHub Releases API Integration Manager
Handles creating GitHub releases, uploading APK/binary assets (all formats), and generating direct download links
with real-time progress bar streaming and automatic asset deduplication.
"""

import os
import mimetypes
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")


class ProgressFileReader:
    """File-like wrapper that reports upload progress to a callback"""
    def __init__(self, file_path, callback=None):
        self.file_path = file_path
        self.total_size = os.path.getsize(file_path)
        self.callback = callback
        self.sent_bytes = 0
        self._f = open(file_path, "rb")

    def read(self, size=-1):
        chunk = self._f.read(size)
        if chunk:
            self.sent_bytes += len(chunk)
            if self.callback:
                try:
                    self.callback(self.sent_bytes, self.total_size)
                except Exception:
                    pass
        return chunk

    def __len__(self):
        return self.total_size

    def seek(self, offset, whence=0):
        self.sent_bytes = 0
        return self._f.seek(offset, whence)

    def close(self):
        if not self._f.closed:
            self._f.close()


class GitHubManager:
    def __init__(self, token=None, owner=None, repo=None):
        self.token = token or GITHUB_TOKEN
        self.owner = owner or GITHUB_OWNER
        self.repo = repo or GITHUB_REPO
        self.api_base = "https://api.github.com"

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "MyStore-Telegram-Admin-Bot"
        }

    def test_connection(self):
        """Verify GitHub token validity and repository access"""
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}"
        try:
            res = requests.get(url, headers=self.headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                return True, f"Connected to {data.get('full_name')} (Stars: {data.get('stargazers_count', 0)})"
            elif res.status_code == 404:
                return False, f"Repository '{self.owner}/{self.repo}' not found or token lacks access."
            else:
                return False, f"GitHub Error {res.status_code}: {res.json().get('message', 'Unknown error')}"
        except Exception as e:
            return False, f"Network connection error: {str(e)}"

    def get_or_create_release(self, tag_name, release_name=None, changelog=None):
        """Create a new GitHub release or retrieve existing one with same tag"""
        clean_tag = str(tag_name).strip().replace(" ", "-")
        release_name = release_name or clean_tag
        changelog = changelog or f"Release {clean_tag} for MyStore"

        # 1. Check if release with tag already exists
        check_url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/tags/{clean_tag}"
        try:
            res = requests.get(check_url, headers=self.headers, timeout=8)
            if res.status_code == 200:
                return True, res.json()
        except Exception:
            pass

        # 2. Check in all releases list in case tag URL mapping was lagging
        try:
            res_list = requests.get(f"{self.api_base}/repos/{self.owner}/{self.repo}/releases?per_page=100", headers=self.headers, timeout=8)
            if res_list.status_code == 200:
                for r in res_list.json():
                    if r.get("tag_name") == clean_tag:
                        return True, r
        except Exception:
            pass

        # 3. Create new release
        create_url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases"
        payload = {
            "tag_name": clean_tag,
            "name": release_name,
            "body": changelog,
            "draft": False,
            "prerelease": False
        }

        try:
            res = requests.post(create_url, headers=self.headers, json=payload, timeout=10)
            if res.status_code in (200, 201):
                return True, res.json()
            elif res.status_code == 422:
                # If already exists, fetch again
                res_retry = requests.get(check_url, headers=self.headers, timeout=8)
                if res_retry.status_code == 200:
                    return True, res_retry.json()
                
                # Check list once more
                res_list = requests.get(f"{self.api_base}/repos/{self.owner}/{self.repo}/releases?per_page=100", headers=self.headers, timeout=8)
                if res_list.status_code == 200:
                    for r in res_list.json():
                        if r.get("tag_name") == clean_tag:
                            return True, r

                err_msg = res.json().get("message", res.text)
                return False, f"Validation Failed: {err_msg}"
            else:
                err_msg = res.json().get("message", res.text)
                return False, f"Failed to create release: {err_msg}"
        except Exception as e:
            return False, f"Exception creating release: {str(e)}"

    def upload_asset(self, release_or_upload_url, file_path, file_name=None, progress_callback=None):
        """Upload binary asset to GitHub Release upload_url with auto-deduplication"""
        if not os.path.exists(file_path):
            return False, f"File not found on device: {file_path}"

        file_name = file_name or os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        if isinstance(release_or_upload_url, dict):
            upload_url = release_or_upload_url.get("upload_url", "")
            release_id = release_or_upload_url.get("id")
            existing_assets = release_or_upload_url.get("assets", [])
        else:
            upload_url = str(release_or_upload_url)
            release_id = None
            existing_assets = []

        # If release_id is present, remove previous asset with the same name to prevent 422 Validation Failed
        if release_id:
            try:
                # Query fresh asset list
                assets_res = requests.get(f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/{release_id}/assets", headers=self.headers, timeout=8)
                if assets_res.status_code == 200:
                    existing_assets = assets_res.json()
                for ast in existing_assets:
                    if ast.get("name") == file_name:
                        ast_id = ast.get("id")
                        requests.delete(f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/assets/{ast_id}", headers=self.headers, timeout=8)
            except Exception as del_err:
                print(f"[GitHub Upload] Notice deleting old asset: {del_err}")

        # Clean GitHub upload URL format
        clean_upload_url = upload_url.split("{")[0] + f"?name={file_name}"

        # Determine MIME type
        content_type = "application/vnd.android.package-archive" if file_name.endswith(".apk") else mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        upload_headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type,
            "Content-Length": str(file_size),
            "User-Agent": "MyStore-Telegram-Admin-Bot"
        }

        reader = ProgressFileReader(file_path, callback=progress_callback)

        try:
            res = requests.post(clean_upload_url, headers=upload_headers, data=reader, timeout=300)

            if res.status_code in (200, 201):
                asset_data = res.json()
                direct_url = asset_data.get("browser_download_url")
                return True, direct_url
            elif res.status_code == 422:
                # If duplicate still exists, extract release ID from upload_url, purge duplicate asset, and retry
                try:
                    parts = upload_url.split("/releases/")
                    if len(parts) > 1:
                        rel_id = parts[1].split("/")[0]
                        assets_res = requests.get(f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/{rel_id}/assets", headers=self.headers, timeout=8)
                        if assets_res.status_code == 200:
                            for a in assets_res.json():
                                if a.get("name") == file_name:
                                    requests.delete(f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/assets/{a.get('id')}", headers=self.headers, timeout=8)
                            
                            # Retry upload
                            reader.seek(0)
                            retry_res = requests.post(clean_upload_url, headers=upload_headers, data=reader, timeout=300)
                            if retry_res.status_code in (200, 201):
                                return True, retry_res.json().get("browser_download_url")
                except Exception as ex:
                    print(f"[GitHub Upload] Retry error: {ex}")

                err_msg = res.json().get("message", res.text)
                return False, f"Upload error {res.status_code}: {err_msg}"
            else:
                err_msg = res.json().get("message", res.text)
                return False, f"Upload error {res.status_code}: {err_msg}"
        except Exception as e:
            return False, f"Asset upload exception: {str(e)}"
        finally:
            reader.close()

    def publish_apk_release(self, file_path, file_name, tag_name, release_name=None, changelog=None, progress_callback=None):
        """Complete workflow: Creates release and uploads file (APK, ZIP, EXE, etc.), returning direct download URL"""
        success, release_or_err = self.get_or_create_release(tag_name, release_name, changelog)
        if not success:
            return False, release_or_err

        return self.upload_asset(release_or_err, file_path, file_name, progress_callback=progress_callback)

    def publish_release_file(self, file_path, file_name, tag_name, release_name=None, changelog=None, progress_callback=None):
        """Alias for publish_apk_release to support all file types"""
        return self.publish_apk_release(file_path, file_name, tag_name, release_name, changelog, progress_callback)

    def list_all_releases(self, per_page=30):
        """List all GitHub releases from repository"""
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases?per_page={per_page}"
        try:
            res = requests.get(url, headers=self.headers, timeout=10)
            if res.status_code == 200:
                return True, res.json()
            else:
                return False, f"GitHub Error {res.status_code}: {res.text}"
        except Exception as e:
            return False, f"Network error: {str(e)}"

    def get_release_by_id(self, release_id):
        """Get details of a specific GitHub release"""
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/{release_id}"
        try:
            res = requests.get(url, headers=self.headers, timeout=10)
            if res.status_code == 200:
                return True, res.json()
            else:
                return False, f"GitHub Error {res.status_code}: {res.text}"
        except Exception as e:
            return False, f"Network error: {str(e)}"

    def delete_release(self, release_id, tag_name=None):
        """Delete a release and its corresponding tag from GitHub"""
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/{release_id}"
        try:
            res = requests.delete(url, headers=self.headers, timeout=10)
            if res.status_code == 204:
                if tag_name:
                    try:
                        ref_url = f"{self.api_base}/repos/{self.owner}/{self.repo}/git/refs/tags/{tag_name}"
                        requests.delete(ref_url, headers=self.headers, timeout=8)
                    except Exception:
                        pass
                return True, "Release successfully deleted from GitHub."
            else:
                return False, f"Failed to delete: {res.text}"
        except Exception as e:
            return False, f"Delete error: {str(e)}"


# Global Singleton Instance
github_mgr = GitHubManager()
