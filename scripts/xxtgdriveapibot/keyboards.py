from telethon import Button
from config import GENERATE_KEY_URL
from helpers import format_bytes, get_mime_icon

def api_key_request_kb():
    """Keyboard shown when user has not configured their API key."""
    return [
        [
            Button.url("🌐 Generate API Key", GENERATE_KEY_URL)
        ],
        [
            Button.inline("❓ How to Generate (Help)", b"help_api_guide")
        ]
    ]

def main_menu_kb(current_folder_id: str = "root", current_folder_name: str = "Root", is_admin: bool = False):
    """Main dashboard menu keyboard."""
    buttons = [
        [
            Button.inline("📁 My Files", b"menu_files:all:1"),
            Button.inline("📤 Upload File", b"menu_upload_guide")
        ],
        [
            Button.inline("🔍 Search Files", b"menu_search_prompt"),
            Button.inline("📂 Folders", b"menu_folders:root")
        ],
        [
            Button.inline("⭐ Starred Files", b"menu_favorites"),
            Button.inline("📊 Storage Stats", b"menu_stats")
        ],
        [
            Button.inline("🗑️ Trash / Bin", b"menu_trash"),
            Button.inline("⚙️ Account Settings", b"menu_account")
        ]
    ]
    if current_folder_id != "root":
        buttons.append([
            Button.inline(f"🔄 Reset Target ({current_folder_name} ➔ Root)", b"funset")
        ])
    if is_admin:
        buttons.append([
            Button.inline("👑 Admin Control Panel", b"admin_panel")
        ])
    return buttons

def files_list_kb(items: list, page: int, total_items: int, per_page: int = 6, folder_id: str = "all"):
    """Keyboard displaying a list of files with pagination buttons."""
    buttons = []
    
    total_pages = max(1, (total_items + per_page - 1) // per_page) if total_items > 0 else 1
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    current_items = items[start_idx:end_idx] if len(items) > per_page else items

    for file in current_items:
        file_id = str(file.get("id") or file.get("message_id"))
        name = file.get("name", "Untitled")
        size_str = format_bytes(file.get("size", 0))
        icon = get_mime_icon(file.get("mimeType", ""), name)
        
        display_name = name if len(name) <= 22 else f"{name[:19]}..."
        button_text = f"{icon} {display_name} ({size_str})"
        data = f"file_view:{file_id}:{folder_id}:{page}".encode('utf-8')
        buttons.append([Button.inline(button_text, data)])

    # Pagination navigation controls
    nav_row = []
    if page > 1:
        nav_row.append(Button.inline("⬅️ Prev", f"menu_files:{folder_id}:{page - 1}".encode('utf-8')))
    else:
        nav_row.append(Button.inline("⏹️", b"noop"))
        
    nav_row.append(Button.inline(f"📄 {page}/{total_pages}", b"noop"))
    
    if page < total_pages:
        nav_row.append(Button.inline("Next ➡️", f"menu_files:{folder_id}:{page + 1}".encode('utf-8')))
    else:
        nav_row.append(Button.inline("⏹️", b"noop"))
        
    buttons.append(nav_row)

    # Actions row
    action_row = [
        Button.inline("📂 Folders", b"menu_folders:root"),
        Button.inline("🔄 Refresh", f"menu_files:{folder_id}:{page}".encode('utf-8')),
        Button.inline("🏠 Main Menu", b"menu_main")
    ]
    buttons.append(action_row)
    
    return buttons

def file_details_kb(file_id: str, is_starred: bool = False, download_url: str = None, folder_id: str = "all", page: int = 1):
    """Keyboard for a single file view with complete management actions."""
    buttons = []
    
    if download_url:
        buttons.append([Button.url("⬇️ Direct Fast Download Link", download_url)])
    
    # Send file directly in chat
    buttons.append([Button.inline("📥 Send File to Chat", f"fsend:{file_id}".encode('utf-8'))])

    # Rename and Move actions
    buttons.append([
        Button.inline("✏️ Rename", f"fren_file_ask:{file_id}:{folder_id}:{page}".encode('utf-8')),
        Button.inline("📦 Move Folder", f"fmove_ask:{file_id}:{folder_id}:{page}".encode('utf-8'))
    ])
    
    star_text = "⭐ Unstar File" if is_starred else "⭐ Star as Favorite"
    star_action = f"file_unstar:{file_id}:{folder_id}:{page}".encode('utf-8') if is_starred else f"file_star:{file_id}:{folder_id}:{page}".encode('utf-8')
    
    buttons.append([
        Button.inline(star_text, star_action),
        Button.inline("🗑️ Delete File", f"file_del_confirm:{file_id}:{folder_id}:{page}".encode('utf-8'))
    ])
    
    buttons.append([
        Button.inline("⬅️ Back to Files", f"menu_files:{folder_id}:{page}".encode('utf-8')),
        Button.inline("🏠 Main Menu", b"menu_main")
    ])
    
    return buttons

def move_file_kb(file_id: str, custom_folders: list, current_folder_id: str = "all", page: int = 1):
    """Keyboard for selecting a destination folder to move a file."""
    buttons = [
        [Button.inline("📁 Root (Saved Messages)", f"fmove_do:{file_id}:root:{current_folder_id}:{page}".encode('utf-8'))]
    ]
    for f in custom_folders:
        f_id = str(f.get("id"))
        f_name = f.get("name", "Folder")
        display_name = f_name if len(f_name) <= 22 else f"{f_name[:19]}..."
        buttons.append([Button.inline(f"📁 {display_name}", f"fmove_do:{file_id}:{f_id}:{current_folder_id}:{page}".encode('utf-8'))])
        
    buttons.append([
        Button.inline("❌ Cancel", f"file_view:{file_id}:{current_folder_id}:{page}".encode('utf-8'))
    ])
    return buttons

def delete_confirm_kb(file_id: str, folder_id: str = "all", page: int = 1):
    """Delete confirmation keyboard."""
    return [
        [
            Button.inline("✅ Yes, Delete Permanently", f"file_del_do:{file_id}:{folder_id}:{page}".encode('utf-8')),
            Button.inline("❌ Cancel", f"file_view:{file_id}:{folder_id}:{page}".encode('utf-8'))
        ]
    ]

def folders_list_kb(custom_folders: list, current_default_id: str = "root", current_parent: str = "root"):
    """Keyboard listing Custom Folders with Active Target markers."""
    buttons = []

    # 1. Custom User Folders (with active indicator)
    for f in custom_folders:
        f_id = str(f.get("id"))
        f_name = f.get("name", "Folder")
        is_default = (f_id == current_default_id)
        tag = " 🎯 [Target]" if is_default else ""
        display_name = f_name if len(f_name) <= 24 else f"{f_name[:21]}..."
        buttons.append([Button.inline(f"📁 {display_name}{tag}", f"fview:{f_id}".encode('utf-8'))])
        
    # 2. Action row
    action_row = [
        Button.inline("➕ Create New Folder", f"folder_create_prompt:{current_parent}".encode('utf-8'))
    ]
    if current_default_id != "root":
        action_row.append(Button.inline("🔄 Reset Target to Root", b"funset"))
    buttons.append(action_row)

    # 3. Navigation controls
    buttons.append([
        Button.inline("🔄 Refresh", f"menu_folders:{current_parent}".encode('utf-8')),
        Button.inline("🏠 Main Menu", b"menu_main")
    ])
    return buttons

def folder_view_kb(folder_id: str, is_current_default: bool = False):
    """Options for an individual folder with Set / Unset default target and Rename."""
    buttons = []
    
    if is_current_default:
        buttons.append([Button.inline("✅ Active Target (Files upload here)", b"noop")])
        buttons.append([Button.inline("🔄 Unset Target (Reset to Root)", b"funset")])
    else:
        buttons.append([Button.inline("📌 Set as Default Target Folder", f"fset:{folder_id}".encode('utf-8'))])

    buttons.append([
        Button.inline("📂 Browse Files in Folder", f"menu_files:{folder_id}:1".encode('utf-8'))
    ])
    buttons.append([
        Button.inline("✏️ Rename Folder", f"fren_fold_ask:{folder_id}".encode('utf-8')),
        Button.inline("🗑️ Delete Folder", f"fdel_confirm:{folder_id}".encode('utf-8'))
    ])
    buttons.append([
        Button.inline("🔙 Back to Folders", b"menu_folders:root")
    ])
    return buttons

def favorites_kb(items: list):
    """Keyboard for favorite / starred files list."""
    buttons = []
    for file in items[:10]:
        file_id = str(file.get("id") or file.get("message_id"))
        name = file.get("name", "Untitled")
        size_str = format_bytes(file.get("size", 0))
        icon = get_mime_icon(file.get("mimeType", ""), name)
        display_name = name if len(name) <= 22 else f"{name[:19]}..."
        buttons.append([Button.inline(f"⭐ {icon} {display_name} ({size_str})", f"file_view:{file_id}:all:1".encode('utf-8'))])
        
    buttons.append([
        Button.inline("🔄 Refresh", b"menu_favorites"),
        Button.inline("🏠 Main Menu", b"menu_main")
    ])
    return buttons

def trash_kb(items: list):
    """Keyboard for trash bin."""
    buttons = []
    for file in items[:8]:
        file_id = str(file.get("id") or file.get("message_id"))
        name = file.get("name", "Deleted File")
        display_name = name if len(name) <= 20 else f"{name[:17]}..."
        buttons.append([
            Button.inline(f"📄 {display_name}", b"noop"),
            Button.inline("♻️ Restore", f"trash_restore:{file_id}".encode('utf-8'))
        ])
        
    if items:
        buttons.append([
            Button.inline("💥 Empty All Trash", b"trash_empty_confirm")
        ])
        
    buttons.append([
        Button.inline("🔄 Refresh", b"menu_trash"),
        Button.inline("🏠 Main Menu", b"menu_main")
    ])
    return buttons

def account_kb():
    """Account & API Key Settings."""
    return [
        [
            Button.inline("🔄 Update / Change API Key", b"menu_setkey_prompt")
        ],
        [
            Button.inline("🚪 Disconnect / Logout", b"menu_logout_confirm")
        ],
        [
            Button.inline("🏠 Back to Main Menu", b"menu_main")
        ]
    ]

def cancel_kb(target_menu: str = "menu_main"):
    """Cancel button."""
    return [[Button.inline("❌ Cancel", target_menu.encode('utf-8'))]]

def back_to_main_kb():
    """Simple back to main menu button."""
    return [[Button.inline("🏠 Back to Main Menu", b"menu_main")]]

def admin_panel_kb():
    """Admin Control Panel Keyboard."""
    return [
        [
            Button.inline("📢 Broadcast Message", b"admin_broadcast_prompt"),
            Button.inline("📊 Global Bot Stats", b"admin_stats")
        ],
        [
            Button.inline("👥 All Users List", b"admin_user_list"),
            Button.inline("🧹 Force Clean Disk", b"admin_clean_disk")
        ],
        [
            Button.inline("🏠 Back to Main Menu", b"menu_main")
        ]
    ]

def admin_stats_kb():
    """Admin Global Statistics Keyboard."""
    return [
        [
            Button.inline("🔄 Refresh Stats", b"admin_stats"),
            Button.inline("📢 Broadcast", b"admin_broadcast_prompt")
        ],
        [
            Button.inline("⬅️ Back to Admin Panel", b"admin_panel")
        ]
    ]

def admin_users_kb():
    """Admin Users List Keyboard."""
    return [
        [
            Button.inline("🔄 Refresh List", b"admin_user_list"),
            Button.inline("📢 Broadcast", b"admin_broadcast_prompt")
        ],
        [
            Button.inline("⬅️ Back to Admin Panel", b"admin_panel")
        ]
    ]

