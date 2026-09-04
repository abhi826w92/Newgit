"""
ImgBB Cloud Image Hosting Manager
Uploads images (photos, documents, base64) to https://api.imgbb.com/1/upload
Returns direct CDN image links for Store Icons & Screenshots.
"""

import os
import requests
from typing import Tuple, Optional


class ImgBBManager:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("IMGBB_API_KEY", "c4ef8823477cbe0dc7c941323e05cbdb")
        self.upload_endpoint = "https://api.imgbb.com/1/upload"

    def upload_image_bytes(self, image_bytes: bytes, filename: str = "image.png", name: Optional[str] = None) -> Tuple[bool, str]:
        """
        Upload binary image bytes to ImgBB.
        Returns: (success: bool, url_or_error: str)
        """
        if not self.api_key:
            return False, "ImgBB API key is not configured."

        try:
            files = {
                'image': (filename, image_bytes)
            }
            params = {
                'key': self.api_key
            }
            if name:
                params['name'] = name

            response = requests.post(self.upload_endpoint, params=params, files=files, timeout=30)
            
            if response.status_code == 200:
                res_data = response.json()
                if res_data.get("success"):
                    data = res_data.get("data", {})
                    direct_url = data.get("url") or data.get("display_url") or data.get("image", {}).get("url")
                    if direct_url:
                        return True, direct_url
                    return False, "No URL found in ImgBB response."
                else:
                    err_msg = res_data.get("error", {}).get("message", "Upload failed.")
                    return False, f"ImgBB Error: {err_msg}"
            else:
                return False, f"ImgBB HTTP Error {response.status_code}: {response.text}"

        except requests.exceptions.Timeout:
            return False, "ImgBB Upload timed out (30s limit)."
        except Exception as e:
            return False, f"ImgBB upload exception: {str(e)}"

    def upload_image_file(self, file_path: str, name: Optional[str] = None) -> Tuple[bool, str]:
        """Upload a local image file path to ImgBB"""
        if not os.path.exists(file_path):
            return False, f"File not found: {file_path}"
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            filename = os.path.basename(file_path)
            return self.upload_image_bytes(content, filename=filename, name=name)
        except Exception as e:
            return False, f"Error reading file {file_path}: {str(e)}"


# Singleton Instance
imgbb_mgr = ImgBBManager()
