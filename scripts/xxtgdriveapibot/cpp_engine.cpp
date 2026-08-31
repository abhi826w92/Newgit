#include <iostream>
#include <vector>
#include <string>
#include <unordered_map>
#include <algorithm>
#include <mutex>
#include <sstream>
#include <cstdint>
#include <cstring>

// High Performance C++ File Record Structure
struct FileRecord {
    std::string id;
    std::string name;
    std::string name_lower;
    int64_t size;
    std::string mime_type;
    std::string parent_id;
    int64_t created_at;
    bool starred;
    std::string raw_json;
};

// Fast User Cache Index
struct UserDataset {
    std::vector<FileRecord> files;
    std::unordered_map<std::string, size_t> id_to_index;
    int64_t last_synced_at;
};

static std::unordered_map<int64_t, UserDataset> g_user_data;
static std::mutex g_mutex;
static thread_local std::string g_result_buffer;

// Fast Helper to lower string
static inline std::string to_lower_fast(const std::string& str) {
    std::string res = str;
    for (char& c : res) {
        if (c >= 'A' && c <= 'Z') c += ('a' - 'A');
    }
    return res;
}

// Fast JSON Escaper
static inline void escape_json_string(std::ostringstream& oss, const std::string& s) {
    oss << '"';
    for (char c : s) {
        switch (c) {
            case '"': oss << "\\\""; break;
            case '\\': oss << "\\\\"; break;
            case '\b': oss << "\\b"; break;
            case '\f': oss << "\\f"; break;
            case '\n': oss << "\\n"; break;
            case '\r': oss << "\\r"; break;
            case '\t': oss << "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    // Control characters
                    char buf[7];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    oss << buf;
                } else {
                    oss << c;
                }
                break;
        }
    }
    oss << '"';
}

static inline bool str_ends_with(const std::string& str, const std::string& suffix) {
    if (str.length() < suffix.length()) return false;
    return str.compare(str.length() - suffix.length(), suffix.length(), suffix) == 0;
}

static inline std::string categorize_mime_fast(const std::string& mime, const std::string& name_lower) {
    if (mime.rfind("video/", 0) == 0 || str_ends_with(name_lower, ".mp4") || str_ends_with(name_lower, ".mkv") || str_ends_with(name_lower, ".avi") || str_ends_with(name_lower, ".mov") || str_ends_with(name_lower, ".flv") || str_ends_with(name_lower, ".webm")) return "videos";
    if (mime.rfind("image/", 0) == 0 || str_ends_with(name_lower, ".jpg") || str_ends_with(name_lower, ".jpeg") || str_ends_with(name_lower, ".png") || str_ends_with(name_lower, ".webp") || str_ends_with(name_lower, ".gif")) return "images";
    if (mime.rfind("audio/", 0) == 0 || str_ends_with(name_lower, ".mp3") || str_ends_with(name_lower, ".m4a") || str_ends_with(name_lower, ".wav") || str_ends_with(name_lower, ".flac") || str_ends_with(name_lower, ".ogg")) return "audio";
    if (str_ends_with(name_lower, ".apk")) return "apks";
    if (mime.rfind("application/pdf", 0) == 0 || mime.rfind("text/", 0) == 0 || str_ends_with(name_lower, ".pdf") || str_ends_with(name_lower, ".doc") || str_ends_with(name_lower, ".docx") || str_ends_with(name_lower, ".txt") || str_ends_with(name_lower, ".zip") || str_ends_with(name_lower, ".rar")) return "documents";
    return "others";
}

extern "C" {

void cpp_clear_user_cache(int64_t user_id) {
    std::lock_guard<std::mutex> lock(g_mutex);
    g_user_data.erase(user_id);
}

void cpp_upsert_file_record(
    int64_t user_id,
    const char* id,
    const char* name,
    int64_t size,
    const char* mime_type,
    const char* parent_id,
    int64_t created_at,
    int32_t starred,
    const char* raw_json
) {
    if (!id || !name) return;
    std::lock_guard<std::mutex> lock(g_mutex);
    auto& udata = g_user_data[user_id];
    std::string s_id = id;
    std::string s_name = name;
    std::string s_name_lower = to_lower_fast(s_name);
    std::string s_mime = mime_type ? mime_type : "";
    std::string s_parent = (parent_id && strlen(parent_id) > 0) ? parent_id : "root";
    std::string s_raw = (raw_json && strlen(raw_json) > 0) ? raw_json : "";

    auto it = udata.id_to_index.find(s_id);
    if (it != udata.id_to_index.end()) {
        // Update existing record
        size_t idx = it->second;
        udata.files[idx].name = s_name;
        udata.files[idx].name_lower = s_name_lower;
        udata.files[idx].size = size;
        udata.files[idx].mime_type = s_mime;
        udata.files[idx].parent_id = s_parent;
        udata.files[idx].created_at = created_at;
        udata.files[idx].starred = (starred != 0);
        if (!s_raw.empty()) udata.files[idx].raw_json = s_raw;
    } else {
        // Insert new record
        FileRecord rec;
        rec.id = s_id;
        rec.name = s_name;
        rec.name_lower = s_name_lower;
        rec.size = size;
        rec.mime_type = s_mime;
        rec.parent_id = s_parent;
        rec.created_at = created_at;
        rec.starred = (starred != 0);
        rec.raw_json = s_raw;
        udata.id_to_index[s_id] = udata.files.size();
        udata.files.push_back(std::move(rec));
    }
}

int32_t cpp_get_total_count(int64_t user_id) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_user_data.find(user_id);
    if (it == g_user_data.end()) return 0;
    return static_cast<int32_t>(it->second.files.size());
}

// Ultra-fast C++ filtering, searching, and pagination returning JSON string
const char* cpp_filter_and_paginate(
    int64_t user_id,
    const char* folder_id,
    const char* search_query,
    int32_t page,
    int32_t per_page
) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_user_data.find(user_id);
    if (it == g_user_data.end() || it->second.files.empty()) {
        g_result_buffer = "{\"status\":\"success\",\"total\":0,\"page\":1,\"total_pages\":1,\"items\":[]}";
        return g_result_buffer.c_str();
    }

    const auto& files = it->second.files;
    std::string s_folder = folder_id ? folder_id : "all";
    std::string s_query = search_query ? to_lower_fast(search_query) : "";
    bool filter_folder = (!s_folder.empty() && s_folder != "all");
    bool filter_search = !s_query.empty();

    std::vector<const FileRecord*> matched;
    matched.reserve(files.size());

    for (const auto& rec : files) {
        if (filter_folder) {
            if (s_folder == "root") {
                if (rec.parent_id != "root" && !rec.parent_id.empty()) continue;
            } else {
                if (rec.parent_id != s_folder) continue;
            }
        }
        if (filter_search) {
            if (rec.name_lower.find(s_query) == std::string::npos && rec.id.find(s_query) == std::string::npos) {
                continue;
            }
        }
        matched.push_back(&rec);
    }

    int32_t total_matched = static_cast<int32_t>(matched.size());
    if (per_page <= 0) per_page = 6;
    int32_t total_pages = std::max(1, (total_matched + per_page - 1) / per_page);
    if (page < 1) page = 1;
    if (page > total_pages) page = total_pages;

    int32_t start_idx = (page - 1) * per_page;
    int32_t end_idx = std::min(start_idx + per_page, total_matched);

    std::ostringstream oss;
    oss << "{\"status\":\"success\","
        << "\"total\":" << total_matched << ","
        << "\"page\":" << page << ","
        << "\"total_pages\":" << total_pages << ","
        << "\"items\":[";

    for (int32_t i = start_idx; i < end_idx; ++i) {
        const auto* rec = matched[i];
        if (i > start_idx) oss << ",";
        if (!rec->raw_json.empty() && rec->raw_json.front() == '{' && rec->raw_json.back() == '}') {
            oss << rec->raw_json;
        } else {
            oss << "{\"id\":";
            escape_json_string(oss, rec->id);
            oss << ",\"name\":";
            escape_json_string(oss, rec->name);
            oss << ",\"size\":" << rec->size;
            oss << ",\"mimeType\":";
            escape_json_string(oss, rec->mime_type);
            oss << ",\"parentId\":";
            escape_json_string(oss, rec->parent_id);
            oss << ",\"folder_id\":";
            escape_json_string(oss, rec->parent_id);
            oss << ",\"created_at\":" << rec->created_at;
            oss << ",\"starred\":" << (rec->starred ? "true" : "false");
            oss << "}";
        }
    }
    oss << "]}";

    g_result_buffer = oss.str();
    return g_result_buffer.c_str();
}

// Ultra-fast C++ Storage & Category Statistics Calculation
const char* cpp_compute_stats(int64_t user_id) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_user_data.find(user_id);
    if (it == g_user_data.end() || it->second.files.empty()) {
        g_result_buffer = "{\"status\":\"success\",\"total_files\":0,\"total_bytes\":0,\"categories\":{}}";
        return g_result_buffer.c_str();
    }

    const auto& files = it->second.files;
    int64_t total_bytes = 0;
    int32_t total_files = static_cast<int32_t>(files.size());

    struct CatStat { int32_t count = 0; int64_t bytes = 0; };
    std::unordered_map<std::string, CatStat> cats;
    cats["videos"] = {};
    cats["images"] = {};
    cats["audio"] = {};
    cats["documents"] = {};
    cats["apks"] = {};
    cats["others"] = {};

    for (const auto& rec : files) {
        total_bytes += rec.size;
        std::string cat = categorize_mime_fast(rec.mime_type, rec.name_lower);
        cats[cat].count++;
        cats[cat].bytes += rec.size;
    }

    std::ostringstream oss;
    oss << "{\"status\":\"success\","
        << "\"total_files\":" << total_files << ","
        << "\"total_bytes\":" << total_bytes << ","
        << "\"categories\":{";

    bool first = true;
    for (const auto& [cat_name, stat] : cats) {
        if (!first) oss << ",";
        first = false;
        oss << "\"" << cat_name << "\":{\"count\":" << stat.count << ",\"bytes\":" << stat.bytes << "}";
    }
    oss << "}}";

    g_result_buffer = oss.str();
    return g_result_buffer.c_str();
}

} // extern "C"
