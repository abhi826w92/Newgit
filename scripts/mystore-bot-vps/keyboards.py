"""
Interactive Inline Keyboards for MyStore Telegram Admin Bot
All navigation and actions are driven via buttons without typing manual commands.
"""

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu_keyboard():
    """Main Control Panel Menu"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add New Project", callback_data="menu:add_project"),
        InlineKeyboardButton("📦 Upload Release to GitHub", callback_data="menu:upload_apk"),
    )
    markup.add(
        InlineKeyboardButton("📋 Manage & Edit Apps", callback_data="menu:list_apps"),
        InlineKeyboardButton("📌 Top & Pinned Apps", callback_data="menu:top_apps"),
    )
    markup.add(
        InlineKeyboardButton("📂 Manage Categories", callback_data="menu:manage_categories"),
        InlineKeyboardButton("🐙 GitHub Releases", callback_data="menu:github_releases"),
    )
    markup.add(
        InlineKeyboardButton("🌟 Set Featured App", callback_data="menu:set_featured"),
        InlineKeyboardButton("📊 Live Analytics", callback_data="menu:analytics"),
    )
    markup.add(
        InlineKeyboardButton("👤 Developer Profile", callback_data="menu:developer_profile"),
        InlineKeyboardButton("💾 Backup & Export JSON", callback_data="menu:backup_export"),
    )
    markup.add(
        InlineKeyboardButton("⚡ Store Health & Ping", callback_data="menu:health_check"),
        InlineKeyboardButton("🗑️ Delete Project", callback_data="menu:delete_app_list"),
    )
    return markup


def project_type_keyboard(categories=None):
    """Select project type or custom category when adding a project"""
    markup = InlineKeyboardMarkup(row_width=1)
    cats = categories or ["Apps", "Web", "Open Source"]

    # 1. Standard project types with Android emoji
    markup.add(
        InlineKeyboardButton("🤖 Android App (APK / Package)", callback_data="type:app:Apps"),
        InlineKeyboardButton("🌐 Web App (PWA / URL)", callback_data="type:web:Web"),
        InlineKeyboardButton("💻 Open Source (GitHub Repo)", callback_data="type:opensource:Open Source")
    )

    # 2. Render all custom categories with folder emoji
    custom_cats = [c for c in cats if c not in ("All", "Apps", "Web", "Open Source", "⭐ Favorites")]
    for c in custom_cats:
        markup.add(
            InlineKeyboardButton(f"📂 {c}", callback_data=f"type:custom:{c}")
        )

    markup.add(
        InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main")
    )
    return markup


def category_selector_keyboard(categories=None):
    """Dynamic Category selector built from database categories with folder emoji"""
    cats = categories or ["Apps", "Web", "Open Source"]
    markup = InlineKeyboardMarkup(row_width=2)
    
    buttons = []
    for c in cats:
        if c in ("All", "⭐ Favorites"):
            continue
        buttons.append(InlineKeyboardButton(f"📂 {c}", callback_data=f"cat:{c}"))

    if buttons:
        markup.add(*buttons)
    markup.add(
        InlineKeyboardButton("❌ Cancel", callback_data="menu:main")
    )
    return markup


def categories_manager_keyboard(categories):
    """List all categories for management with lock on system and folder on custom"""
    markup = InlineKeyboardMarkup(row_width=2)
    
    buttons = []
    for c in categories:
        if c in ("All", "⭐ Favorites"):
            buttons.append(InlineKeyboardButton(f"🔒 {c}", callback_data=f"cat_info:{c}"))
        else:
            buttons.append(InlineKeyboardButton(f"📂 {c}", callback_data=f"cat_manage:{c}"))

    if buttons:
        markup.add(*buttons)

    markup.add(
        InlineKeyboardButton("➕ Add New Category", callback_data="cat:add_new"),
        InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main")
    )
    return markup


def category_item_keyboard(category_name, is_first=False, is_last=False, is_system=False):
    """Action menu for a single category"""
    markup = InlineKeyboardMarkup(row_width=2)

    if is_system:
        markup.add(InlineKeyboardButton("🔒 System Category (Read Only)", callback_data="cat_info:sys"))
    else:
        # Move buttons
        nav_buttons = []
        if not is_first:
            nav_buttons.append(InlineKeyboardButton("⬅️ Move Left", callback_data=f"cat_move:{category_name}:up"))
        if not is_last:
            nav_buttons.append(InlineKeyboardButton("➡️ Move Right", callback_data=f"cat_move:{category_name}:down"))
        if nav_buttons:
            markup.add(*nav_buttons)

        markup.add(
            InlineKeyboardButton("✏️ Rename", callback_data=f"cat_rename:{category_name}"),
            InlineKeyboardButton("🗑️ Delete", callback_data=f"cat_delete_confirm:{category_name}"),
        )

    markup.add(InlineKeyboardButton("🔙 Back to Categories", callback_data="menu:manage_categories"))
    return markup


def confirm_delete_category_keyboard(category_name):
    """Confirm category deletion"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("⚠️ Yes, Delete Category", callback_data=f"cat_delete_exec:{category_name}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cat_manage:{category_name}"),
    )
    return markup


def app_list_keyboard(apps, action_prefix="view_app"):
    """Generate dynamic list of all apps as inline buttons"""
    markup = InlineKeyboardMarkup(row_width=1)
    
    for app in apps:
        app_id = app.get("id", "")
        name = app.get("name", "Unnamed App")
        app_type = app.get("type", "app")
        pinned_tag = f"📌 #{app.get('pinnedOrder', 1)} " if app.get("pinned") else ""
        badge = f"[{app.get('badge')}] " if app.get("badge") else ""
        featured = "🌟 " if app.get("featured") else ""
        
        # Smart type icon determination based on type, category, and name
        app_type_str = str(app_type or "").lower()
        category_str = str(app.get("category", "")).lower()
        name_str = str(name or "").lower()
        
        if app_type_str in ("bot", "tgbot", "tgbots") or "bot" in category_str or "bot" in name_str:
            type_icon = "🤖"
        elif app_type_str in ("web", "webapp") or "web" in category_str:
            type_icon = "🌐"
        elif app_type_str in ("opensource", "source", "github") or "open source" in category_str:
            type_icon = "💻"
        elif app_type_str in ("game", "games") or "game" in category_str:
            type_icon = "🎮"
        elif app_type_str in ("mod", "mods") or "mod" in category_str:
            type_icon = "📦"
        else:
            type_icon = "📱"
        
        button_text = f"{pinned_tag}{featured}{badge}{type_icon} {name} ({app.get('version', 'v1.0')})"
        markup.add(InlineKeyboardButton(button_text, callback_data=f"{action_prefix}:{app_id}"))

    markup.add(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main"))
    return markup


def is_valid_telegram_button_url(url):
    """Validate if URL can be safely attached to a Telegram InlineKeyboardButton without error"""
    if not url:
        return False
    u = str(url).strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    if "localhost" in u or "127.0.0.1" in u or "0.0.0.0" in u:
        return False
    return True


def app_detail_actions_keyboard(app_id, web_url="http://localhost:8080", is_pinned=False):
    """Actions available for a single app"""
    markup = InlineKeyboardMarkup(row_width=2)
    pin_text = "📌 Unpin from Top" if is_pinned else "📌 Pin to Top"
    pin_action = f"unpin_app:{app_id}" if is_pinned else f"pin_app:{app_id}"

    markup.add(
        InlineKeyboardButton("🚀 Upload New Update / Release", callback_data=f"update_app_release:{app_id}")
    )
    markup.add(
        InlineKeyboardButton("✏️ Edit Fields & Links", callback_data=f"edit_menu:{app_id}"),
        InlineKeyboardButton("🖼️ Manage Screenshots", callback_data=f"shots_menu:{app_id}"),
    )
    markup.add(
        InlineKeyboardButton("🏷️ Set Badge / Tag", callback_data=f"set_badge_menu:{app_id}"),
        InlineKeyboardButton(pin_text, callback_data=pin_action),
    )
    markup.add(
        InlineKeyboardButton("🌟 Toggle Featured", callback_data=f"feature_app:{app_id}"),
        InlineKeyboardButton("🗑️ Delete App", callback_data=f"confirm_delete:{app_id}"),
    )

    last_row = []
    full_url = f"{web_url.rstrip('/')}/#/apps/{app_id}" if web_url else ""
    if is_valid_telegram_button_url(full_url):
        last_row.append(InlineKeyboardButton("🌐 Open in Store", url=full_url))
    else:
        last_row.append(InlineKeyboardButton("🔗 Copy Web Link", callback_data=f"copy_url:{app_id}"))

    last_row.append(InlineKeyboardButton("🔙 Back to App List", callback_data="menu:list_apps"))
    markup.add(*last_row)
    return markup


def top_apps_manager_keyboard(pinned_apps):
    """List of all apps currently pinned to Top with ranking numbers"""
    markup = InlineKeyboardMarkup(row_width=1)
    
    for app in pinned_apps:
        app_id = app.get("id")
        order = app.get("pinnedOrder", 1)
        name = app.get("name", "App")
        markup.add(InlineKeyboardButton(f"📌 #{order}: {name}", callback_data=f"top_app_manage:{app_id}"))

    markup.add(
        InlineKeyboardButton("➕ Pin Another App to Top", callback_data="menu:pin_pick_app"),
        InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main"),
    )
    return markup


def top_app_action_keyboard(app_id, rank, is_first, is_last):
    """Actions for a specific Top/Pinned app (Move Up, Move Down, Edit, Remove)"""
    markup = InlineKeyboardMarkup(row_width=2)
    
    # Move buttons
    nav_buttons = []
    if not is_first:
        nav_buttons.append(InlineKeyboardButton("⬆️ Move Up (Rank)", callback_data=f"top_move:{app_id}:up"))
    if not is_last:
        nav_buttons.append(InlineKeyboardButton("⬇️ Move Down", callback_data=f"top_move:{app_id}:down"))
    if nav_buttons:
        markup.add(*nav_buttons)

    markup.add(
        InlineKeyboardButton("✏️ Quick Edit", callback_data=f"edit_menu:{app_id}"),
        InlineKeyboardButton("🏷️ Set Badge/Tag", callback_data=f"set_badge_menu:{app_id}"),
    )
    markup.add(
        InlineKeyboardButton("🖼️ Screenshots", callback_data=f"shots_menu:{app_id}"),
        InlineKeyboardButton("🗑️ Delete App", callback_data=f"confirm_delete:{app_id}"),
    )
    markup.add(
        InlineKeyboardButton("❌ Remove from Top", callback_data=f"top_unpin:{app_id}"),
        InlineKeyboardButton("🔙 Back to Top List", callback_data="menu:top_apps"),
    )
    return markup


def app_edit_menu_keyboard(app_id):
    """Sub-menu for editing individual fields of an app in real-time"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📝 Edit Name", callback_data=f"field:name:{app_id}"),
        InlineKeyboardButton("💬 Edit Tagline", callback_data=f"field:tagline:{app_id}"),
    )
    markup.add(
        InlineKeyboardButton("📄 Edit Description", callback_data=f"field:desc:{app_id}"),
        InlineKeyboardButton("🏷️ Change Category", callback_data=f"field:cat:{app_id}"),
    )
    markup.add(
        InlineKeyboardButton("🎨 Change Icon URL", callback_data=f"field:icon:{app_id}"),
        InlineKeyboardButton("🔗 Change Target URL", callback_data=f"field:url:{app_id}"),
    )
    markup.add(
        InlineKeyboardButton("📦 Bump Version", callback_data=f"bump_ver:{app_id}"),
        InlineKeyboardButton("💾 Change File Size", callback_data=f"field:size:{app_id}"),
    )
    markup.add(
        InlineKeyboardButton("🔘 Edit Button Text", callback_data=f"field:buttonText:{app_id}"),
    )
    markup.add(
        InlineKeyboardButton("🔙 Back to App Details", callback_data=f"view_app:{app_id}")
    )
    return markup


def badge_selection_keyboard(app_id):
    """Badge / Tag selection menu"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✨ NEW", callback_data=f"badge_pick:{app_id}:new"),
        InlineKeyboardButton("🔥 UPDATED", callback_data=f"badge_pick:{app_id}:updated"),
    )
    markup.add(
        InlineKeyboardButton("⭐ FEATURED", callback_data=f"badge_pick:{app_id}:featured"),
        InlineKeyboardButton("🚀 BETA", callback_data=f"badge_pick:{app_id}:beta"),
    )
    markup.add(
        InlineKeyboardButton("⚡ POPULAR", callback_data=f"badge_pick:{app_id}:popular"),
        InlineKeyboardButton("❌ Clear Badge", callback_data=f"badge_apply:{app_id}:clear:0"),
    )
    markup.add(
        InlineKeyboardButton("🔙 Back to App", callback_data=f"view_app:{app_id}")
    )
    return markup


def badge_duration_keyboard(app_id, badge_code):
    """Badge duration / time limit selection"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("⏱️ 24 Hours (1 Day)", callback_data=f"badge_apply:{app_id}:{badge_code}:1"),
        InlineKeyboardButton("⏱️ 3 Days", callback_data=f"badge_apply:{app_id}:{badge_code}:3"),
    )
    markup.add(
        InlineKeyboardButton("⏱️ 7 Days (1 Week)", callback_data=f"badge_apply:{app_id}:{badge_code}:7"),
        InlineKeyboardButton("⏱️ 14 Days (2 Weeks)", callback_data=f"badge_apply:{app_id}:{badge_code}:14"),
    )
    markup.add(
        InlineKeyboardButton("♾️ Permanent (No Expiry)", callback_data=f"badge_apply:{app_id}:{badge_code}:0"),
    )
    markup.add(
        InlineKeyboardButton("🔙 Back to Badges", callback_data=f"set_badge_menu:{app_id}")
    )
    return markup


def screenshots_manager_keyboard(app_id, shots_count=0):
    """Sub-menu for screenshot operations"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add Screenshot Link(s)", callback_data=f"shot_add:{app_id}"),
        InlineKeyboardButton("📸 Upload Photo", callback_data=f"shot_photo:{app_id}"),
    )
    if shots_count > 0:
        markup.add(
            InlineKeyboardButton(f"🗑️ Delete Single ({shots_count})", callback_data=f"shot_del_list:{app_id}"),
            InlineKeyboardButton("🧹 Clear All", callback_data=f"shot_clear:{app_id}"),
        )
    markup.add(
        InlineKeyboardButton("🔙 Back to App Details", callback_data=f"view_app:{app_id}")
    )
    return markup


def delete_screenshot_list_keyboard(app_id, screenshots):
    """List screenshots as clickable buttons to delete"""
    markup = InlineKeyboardMarkup(row_width=1)
    for idx, url in enumerate(screenshots):
        short_url = url.split('/')[-1][:30] or f"Screenshot #{idx+1}"
        markup.add(InlineKeyboardButton(f"🗑️ #{idx+1}: {short_url}", callback_data=f"shot_remove:{app_id}:{idx}"))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"shots_menu:{app_id}"))
    return markup


def github_releases_list_keyboard(releases):
    """List all GitHub Releases"""
    markup = InlineKeyboardMarkup(row_width=1)
    
    for rel in releases:
        rel_id = rel.get("id")
        tag = rel.get("tag_name", "v1.0")
        name = rel.get("name") or tag
        assets_count = len(rel.get("assets", []))
        
        btn_text = f"📦 {tag} - {name[:25]} ({assets_count} files)"
        markup.add(InlineKeyboardButton(btn_text, callback_data=f"gh_rel:{rel_id}"))

    markup.add(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main"))
    return markup


def github_release_detail_keyboard(release_id, tag_name, download_url=None, html_url=None):
    """Actions for a single GitHub release"""
    markup = InlineKeyboardMarkup(row_width=2)
    
    url_row = []
    if is_valid_telegram_button_url(download_url):
        url_row.append(InlineKeyboardButton("📥 Direct Download", url=download_url))
    if is_valid_telegram_button_url(html_url):
        url_row.append(InlineKeyboardButton("🐙 GitHub Page", url=html_url))
    if url_row:
        markup.add(*url_row)
        
    markup.add(
        InlineKeyboardButton("🗑️ Delete This Release", callback_data=f"gh_del_confirm:{release_id}:{tag_name}"),
        InlineKeyboardButton("🔙 Back to Releases", callback_data="menu:github_releases")
    )
    return markup


def confirm_delete_github_release_keyboard(release_id, tag_name):
    """Safety confirmation before deleting GitHub release"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("⚠️ Yes, Delete from GitHub", callback_data=f"gh_del_exec:{release_id}:{tag_name}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"gh_rel:{release_id}"),
    )
    return markup


def confirm_delete_keyboard(app_id):
    """Safety confirmation before deleting an app"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("⚠️ Yes, Delete", callback_data=f"do_delete:{app_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"view_app:{app_id}"),
    )
    return markup


def developer_profile_keyboard(dev_data):
    """Developer profile actions"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✏️ Edit Name / Handle", callback_data="dev:edit_name"),
        InlineKeyboardButton("📝 Edit Bio / Tagline", callback_data="dev:edit_bio"),
    )
    markup.add(
        InlineKeyboardButton("🌐 Edit Portfolio URL", callback_data="dev:edit_portfolio"),
        InlineKeyboardButton("💬 Edit Telegram Link", callback_data="dev:edit_telegram"),
    )
    markup.add(
        InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main")
    )
    return markup


def export_options_keyboard():
    """Secure Database & Store Export Options Menu"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📄 Full JSON Database", callback_data="export:full_json"),
        InlineKeyboardButton("📊 Apps Spreadsheet (CSV)", callback_data="export:csv"),
    )
    markup.add(
        InlineKeyboardButton("📈 Analytics Data (JSON)", callback_data="export:analytics_json"),
        InlineKeyboardButton("☁️ Archive to Channel", callback_data="export:channel_archive"),
    )
    markup.add(
        InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main")
    )
    return markup


def back_to_main_keyboard():
    """Simple back button"""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="menu:main"))
    return markup


def cancel_wizard_keyboard():
    """Cancel button during step-by-step wizard"""
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ Cancel & Return to Menu", callback_data="menu:main"))
    return markup


def skip_step_keyboard(skip_callback="wizard_skip:screenshots"):
    """Keyboard with a Skip button and Cancel button during wizard"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("⏭️ Skip this Step", callback_data=skip_callback),
        InlineKeyboardButton("❌ Cancel", callback_data="menu:main"),
    )
    return markup


def screenshots_step_keyboard(has_screenshots=False):
    """Keyboard with Finish or Skip buttons during multi-screenshot upload"""
    markup = InlineKeyboardMarkup(row_width=2)
    if has_screenshots:
        markup.add(
            InlineKeyboardButton("🚀 Finish & Publish Project", callback_data="wizard_finish:publish"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu:main"),
        )
    else:
        markup.add(
            InlineKeyboardButton("⏭️ Skip this Step", callback_data="wizard_skip:screenshots"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu:main"),
        )
    return markup
