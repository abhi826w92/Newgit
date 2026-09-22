/**
 * MyStore Ultra High-Performance C++ Core Engine
 * Provides microsecond search scoring, priority ranking sorting, and checksum hashing.
 */

#include <iostream>
#include <vector>
#include <string>
#include <algorithm>
#include <cstring>
#include <cmath>
#include <cstdint>

extern "C" {

/**
 * High-speed Levenshtein Fuzzy String Match Score (0.0 to 1.0)
 */
double cpp_fuzzy_score(const char* s1_cstr, const char* s2_cstr) {
    if (!s1_cstr || !s2_cstr) return 0.0;
    std::string s1(s1_cstr);
    std::string s2(s2_cstr);

    // Transform to lower case
    std::transform(s1.begin(), s1.end(), s1.begin(), ::tolower);
    std::transform(s2.begin(), s2.end(), s2.begin(), ::tolower);

    if (s1 == s2) return 1.0;
    if (s1.empty() || s2.empty()) return 0.0;

    // Direct substring match bonus
    if (s1.find(s2) != std::string::npos || s2.find(s1) != std::string::npos) {
        double len_ratio = (double)std::min(s1.length(), s2.length()) / (double)std::max(s1.length(), s2.length());
        return 0.85 + (0.15 * len_ratio);
    }

    const size_t len1 = s1.size(), len2 = s2.size();
    std::vector<size_t> col(len2 + 1), prevCol(len2 + 1);

    for (size_t i = 0; i < prevCol.size(); i++) prevCol[i] = i;

    for (size_t i = 0; i < len1; i++) {
        col[0] = i + 1;
        for (size_t j = 0; j < len2; j++) {
            col[j + 1] = std::min({ 
                prevCol[1 + j] + 1, 
                col[j] + 1, 
                prevCol[j] + (s1[i] == s2[j] ? 0 : 1) 
            });
        }
        col.swap(prevCol);
    }

    size_t dist = prevCol[len2];
    size_t max_len = std::max(len1, len2);
    return 1.0 - ((double)dist / (double)max_len);
}

/**
 * Fast CRC32 Checksum for File Integrity Verification
 */
uint32_t cpp_crc32(const uint8_t* data, size_t length) {
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < length; ++i) {
        uint8_t byte = data[i];
        crc ^= byte;
        for (int j = 0; j < 8; ++j) {
            uint32_t mask = -(crc & 1);
            crc = (crc >> 1) ^ (0xEDB88320 & mask);
        }
    }
    return ~crc;
}

/**
 * Benchmark & Engine Health Check
 */
int cpp_engine_ping() {
    return 200;
}

} // extern "C"
