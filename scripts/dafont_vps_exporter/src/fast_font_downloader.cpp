#include <iostream>
#include <fstream>
#include <string>
#include <vector>
#include <queue>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <chrono>
#include <regex>
#include <cstring>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <curl/curl.h>
#include <sqlite3.h>
#include <zip.h>

std::string g_db_path = "fonts_index.db";
std::string g_output_dir = "dafont_archive";
double g_min_free_disk_gb = 2.0;
const char* USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";

std::atomic<bool> g_running(true);
std::atomic<uint64_t> g_total_downloaded(0);
std::atomic<uint64_t> g_total_fonts_extracted(0);
std::atomic<uint64_t> g_total_bytes_downloaded(0);
std::atomic<uint64_t> g_failed_count(0);

double get_free_disk_gb(const std::string& path = ".") {
    struct statvfs stat;
    if (statvfs(path.c_str(), &stat) != 0) return 100.0;
    return (double)(stat.f_bavail * stat.f_frsize) / (1024.0 * 1024.0 * 1024.0);
}

void mkdir_p(const std::string& path) {
    char tmp[512];
    snprintf(tmp, sizeof(tmp), "%s", path.c_str());
    size_t len = strlen(tmp);
    if (tmp[len - 1] == '/') tmp[len - 1] = 0;
    for (char* p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = 0;
            mkdir(tmp, 0755);
            *p = '/';
        }
    }
    mkdir(tmp, 0755);
}

static size_t WriteMemoryCallback(void* contents, size_t size, size_t nmemb, void* userp) {
    size_t total = size * nmemb;
    std::string* mem = (std::string*)userp;
    mem->append((char*)contents, total);
    return total;
}

bool http_get(CURL* curl, const std::string& url, std::string& buffer) {
    buffer.clear();
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteMemoryCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, (void*)&buffer);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, USER_AGENT);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15L);
    curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "gzip, deflate");
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    CURLcode res = curl_easy_perform(curl);
    return (res == CURLE_OK);
}

sqlite3* open_db() {
    sqlite3* db;
    if (sqlite3_open(g_db_path.c_str(), &db) != SQLITE_OK) return nullptr;
    sqlite3_exec(db, "PRAGMA synchronous = OFF;", nullptr, nullptr, nullptr);
    sqlite3_exec(db, "PRAGMA journal_mode = WAL;", nullptr, nullptr, nullptr);
    const char* sql = "CREATE TABLE IF NOT EXISTS fonts ("
                      "slug TEXT PRIMARY KEY, "
                      "source TEXT, "
                      "status TEXT DEFAULT 'pending', "
                      "font_files INTEGER DEFAULT 0, "
                      "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);";
    sqlite3_exec(db, sql, nullptr, nullptr, nullptr);
    return db;
}

int extract_fonts_from_zip(const std::string& slug, const std::string& zip_data, const std::string& dest_dir) {
    if (zip_data.size() < 22) return 0;
    
    zip_error_t ziperror;
    zip_error_init(&ziperror);
    zip_source_t* src = zip_source_buffer_create(zip_data.data(), zip_data.size(), 0, &ziperror);
    if (!src) return 0;

    zip_t* za = zip_open_from_source(src, ZIP_RDONLY, &ziperror);
    if (!za) {
        zip_source_free(src);
        return 0;
    }

    mkdir_p(dest_dir);
    zip_int64_t num_entries = zip_get_num_entries(za, 0);
    int extracted_count = 0;

    for (zip_uint64_t i = 0; i < (zip_uint64_t)num_entries; i++) {
        const char* name = zip_get_name(za, i, 0);
        if (!name) continue;
        
        std::string fname(name);
        size_t slash_pos = fname.find_last_of("/\\");
        std::string base_name = (slash_pos != std::string::npos) ? fname.substr(slash_pos + 1) : fname;
        if (base_name.empty()) continue;

        std::string lower_name = base_name;
        for (auto& c : lower_name) c = tolower(c);
        
        bool is_font = (lower_name.rfind(".ttf") == lower_name.size() - 4) ||
                       (lower_name.rfind(".otf") == lower_name.size() - 4) ||
                       (lower_name.rfind(".woff") == lower_name.size() - 5) ||
                       (lower_name.rfind(".woff2") == lower_name.size() - 6);

        if (!is_font) continue;

        zip_file_t* zf = zip_fopen_index(za, i, 0);
        if (!zf) continue;

        std::string out_filepath = dest_dir + "/" + base_name;
        std::ofstream out(out_filepath, std::ios::binary);
        if (out.is_open()) {
            char buffer[8192];
            zip_int64_t bytes_read;
            while ((bytes_read = zip_fread(zf, buffer, sizeof(buffer))) > 0) {
                out.write(buffer, bytes_read);
            }
            out.close();
            extracted_count++;
        }
        zip_fclose(zf);
    }

    zip_close(za);
    return extracted_count;
}

void crawl_all_letters() {
    sqlite3* db = open_db();
    if (!db) return;
    
    CURL* curl = curl_easy_init();
    std::vector<std::string> letters;
    for (char c = 'a'; c <= 'z'; c++) letters.push_back(std::string(1, c));
    letters.push_back("ot_1");

    std::regex dl_regex("href=\"//dl\\.dafont\\.com/dl/\\?f=([a-z0-9_\\-]+)\"");
    std::string html;
    uint64_t total_indexed = 0;

    std::cout << "\n======================================================\n";
    std::cout << "[*] C++ Ultra-Fast Crawler: Indexing full DaFont Alphabet...\n";
    std::cout << "======================================================\n";

    for (const auto& letter : letters) {
        int page = 1;
        int consecutive_empty = 0;
        int letter_total = 0;

        while (consecutive_empty < 2) {
            std::string url = "https://www.dafont.com/alpha.php?lettre=" + letter + "&page=" + std::to_string(page);
            if (!http_get(curl, url, html)) break;

            auto words_begin = std::sregex_iterator(html.begin(), html.end(), dl_regex);
            auto words_end = std::sregex_iterator();
            int page_count = 0;

            sqlite3_exec(db, "BEGIN TRANSACTION;", nullptr, nullptr, nullptr);
            sqlite3_stmt* stmt;
            sqlite3_prepare_v2(db, "INSERT OR IGNORE INTO fonts (slug, source) VALUES (?, ?);", -1, &stmt, nullptr);

            for (std::sregex_iterator i = words_begin; i != words_end; ++i) {
                std::smatch match = *i;
                std::string slug = match[1].str();
                sqlite3_bind_text(stmt, 1, slug.c_str(), -1, SQLITE_TRANSIENT);
                std::string src = "alpha_" + letter;
                sqlite3_bind_text(stmt, 2, src.c_str(), -1, SQLITE_TRANSIENT);
                sqlite3_step(stmt);
                sqlite3_reset(stmt);
                page_count++;
            }
            sqlite3_finalize(stmt);
            sqlite3_exec(db, "COMMIT;", nullptr, nullptr, nullptr);

            if (page_count == 0) {
                consecutive_empty++;
            } else {
                consecutive_empty = 0;
                letter_total += page_count;
                total_indexed += page_count;
                if (page % 10 == 0) {
                    std::cout << "  [Letter " << letter << "] Page " << page << " | +" << page_count << " fonts | Total: " << total_indexed << "\n";
                }
            }
            page++;
        }
        std::cout << "[✓] Section '" << letter << "' finished: " << letter_total << " fonts indexed.\n";
    }

    curl_easy_cleanup(curl);
    sqlite3_close(db);
    std::cout << "\n[✓] Indexing Complete! Total Indexed: " << total_indexed << " fonts.\n";
}

void download_worker_thread(std::queue<std::string>& queue, std::mutex& mtx, std::condition_variable& cv, sqlite3* db, std::mutex& db_mtx) {
    CURL* curl = curl_easy_init();
    std::string zip_buffer;

    while (g_running) {
        std::string slug;
        {
            std::unique_lock<std::mutex> lock(mtx);
            if (queue.empty()) break;
            slug = queue.front();
            queue.pop();
        }

        std::string url = "https://dl.dafont.com/dl/?f=" + slug;
        bool downloaded = false;
        int extracted = 0;

        for (int attempt = 0; attempt < 3; attempt++) {
            if (http_get(curl, url, zip_buffer)) {
                if (zip_buffer.size() >= 22 && zip_buffer[0] == 'P' && zip_buffer[1] == 'K') {
                    std::string font_dir = g_output_dir + "/" + slug;
                    extracted = extract_fonts_from_zip(slug, zip_buffer, font_dir);
                    if (extracted > 0) {
                        downloaded = true;
                        g_total_bytes_downloaded += zip_buffer.size();
                        g_total_fonts_extracted += extracted;
                        g_total_downloaded++;
                        break;
                    }
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

        if (!downloaded) g_failed_count++;

        {
            std::lock_guard<std::mutex> lock(db_mtx);
            sqlite3_stmt* stmt;
            const char* sql = "UPDATE fonts SET status = ?, font_files = ?, updated_at = CURRENT_TIMESTAMP WHERE slug = ?;";
            if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) == SQLITE_OK) {
                sqlite3_bind_text(stmt, 1, downloaded ? "downloaded" : "failed", -1, SQLITE_STATIC);
                sqlite3_bind_int(stmt, 2, extracted);
                sqlite3_bind_text(stmt, 3, slug.c_str(), -1, SQLITE_TRANSIENT);
                sqlite3_step(stmt);
                sqlite3_finalize(stmt);
            }
        }
    }
    curl_easy_cleanup(curl);
}

void start_mass_download(int limit, int num_threads) {
    double free_gb = get_free_disk_gb(g_output_dir);
    if (free_gb < g_min_free_disk_gb) {
        std::cerr << "[!] Low disk space: " << free_gb << " GB free. Aborting.\n";
        return;
    }

    sqlite3* db = open_db();
    if (!db) return;

    std::queue<std::string> task_queue;
    std::string query = "SELECT slug FROM fonts WHERE status = 'pending'";
    if (limit > 0) query += " LIMIT " + std::to_string(limit);
    query += ";";

    sqlite3_stmt* stmt;
    if (sqlite3_prepare_v2(db, query.c_str(), -1, &stmt, nullptr) == SQLITE_OK) {
        while (sqlite3_step(stmt) == SQLITE_ROW) {
            const char* text = (const char*)sqlite3_column_text(stmt, 0);
            if (text) task_queue.push(std::string(text));
        }
        sqlite3_finalize(stmt);
    }

    uint64_t total_tasks = task_queue.size();
    if (total_tasks == 0) {
        std::cout << "[*] No pending fonts in database. Run --crawl first.\n";
        sqlite3_close(db);
        return;
    }

    std::cout << "\n======================================================\n";
    std::cout << "[*] C++ Native High-Performance Font Downloader\n";
    std::cout << "[*] Target Queue: " << total_tasks << " fonts | Worker Threads: " << num_threads << "\n";
    std::cout << "[*] Available Disk Space: " << free_gb << " GB\n";
    std::cout << "======================================================\n";

    mkdir_p(g_output_dir);
    std::mutex queue_mtx;
    std::mutex db_mtx;
    std::condition_variable cv;

    auto start_time = std::chrono::steady_clock::now();

    std::vector<std::thread> workers;
    for (int i = 0; i < num_threads; i++) {
        workers.emplace_back(download_worker_thread, std::ref(task_queue), std::ref(queue_mtx), std::ref(cv), db, std::ref(db_mtx));
    }

    while (g_running) {
        std::this_thread::sleep_for(std::chrono::seconds(2));
        uint64_t done = g_total_downloaded + g_failed_count;
        auto now = std::chrono::steady_clock::now();
        double elapsed = std::chrono::duration_cast<std::chrono::duration<double>>(now - start_time).count();
        double rate = (elapsed > 0) ? (done / elapsed) : 0;
        double mb = (double)g_total_bytes_downloaded / (1024.0 * 1024.0);
        double mb_s = (elapsed > 0) ? (mb / elapsed) : 0;

        std::cout << "[HUD] " << done << "/" << total_tasks
                  << " (" << (done * 100.0 / total_tasks) << "%) | "
                  << rate << " fonts/s (" << mb_s << " MB/s) | "
                  << "Extracted: " << g_total_fonts_extracted << " font files\n";

        double current_free = get_free_disk_gb(g_output_dir);
        if (current_free < g_min_free_disk_gb) {
            std::cerr << "\n[!] Storage safety threshold reached (" << current_free << " GB free). Halting.\n";
            g_running = false;
            break;
        }

        if (done >= total_tasks) break;
    }

    for (auto& t : workers) {
        if (t.joinable()) t.join();
    }

    auto end_time = std::chrono::steady_clock::now();
    double total_sec = std::chrono::duration_cast<std::chrono::duration<double>>(end_time - start_time).count();

    sqlite3_close(db);

    std::cout << "\n======================================================\n";
    std::cout << "[✓] MASS EXPORT COMPLETE in " << total_sec << " seconds!\n";
    std::cout << "[✓] Downloaded Packages: " << g_total_downloaded << "\n";
    std::cout << "[✓] Total Font Files Extracted: " << g_total_fonts_extracted << " (.ttf, .otf)\n";
    std::cout << "[✓] Average Speed: " << (g_total_downloaded / (total_sec > 0 ? total_sec : 1)) << " fonts/sec\n";
    std::cout << "======================================================\n";
}

int main(int argc, char* argv[]) {
    curl_global_init(CURL_GLOBAL_ALL);
    
    bool do_crawl = false;
    bool do_download = false;
    int limit = 0;
    int threads = 64;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--crawl") == 0) do_crawl = true;
        else if (strcmp(argv[i], "--download") == 0) do_download = true;
        else if (strcmp(argv[i], "--limit") == 0 && i + 1 < argc) limit = atoi(argv[++i]);
        else if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) threads = atoi(argv[++i]);
        else if (strcmp(argv[i], "--out") == 0 && i + 1 < argc) g_output_dir = argv[++i];
        else if (strcmp(argv[i], "--db") == 0 && i + 1 < argc) g_db_path = argv[++i];
    }

    if (do_crawl) {
        crawl_all_letters();
    }
    if (do_download) {
        start_mass_download(limit, threads);
    }
    if (!do_crawl && !do_download) {
        std::cout << "Usage: " << argv[0] << " [--crawl] [--download] [--limit N] [--threads 64] [--out dir] [--db file.db]\n";
    }

    curl_global_cleanup();
    return 0;
}
