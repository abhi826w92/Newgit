package com.example.multistreamwrap

data class Site(val name: String, val url: String) {
    val domain: String get() = android.net.Uri.parse(url).host ?: ""
}

data class Category(val name: String, val sites: List<Site>)

object SitesProvider {
    val categories: List<Category> by lazy {
        parseCategories(bestSitesData())
    }

    private fun bestSitesData(): String {
        return try {
            val native = NativeLib.getSitesData()
            if (native.isNotBlank()) native else SiteData.sites
        } catch (_: Exception) {
            SiteData.sites
        }
    }

    private fun parseCategories(data: String): List<Category> {
        if (data.isNullOrBlank()) return FALLBACK_CATEGORIES
        return try {
            val result = mutableListOf<Category>()
            var currentName: String? = null
            var currentSites = mutableListOf<Site>()
            for (raw in data.split('\n')) {
                val line = raw.trim()
                if (line.isEmpty() || line.startsWith("#")) continue
                val parts = line.split('|')
                when {
                    parts.size >= 2 && parts[0] == "CATEGORY" && parts[1].isNotBlank() -> {
                        flush(result, currentName, currentSites)
                        currentName = parts[1]
                        currentSites = mutableListOf()
                    }
                    parts.size >= 3 && parts[0] == "SITE" && parts[1].isNotBlank() && parts[2].isNotBlank() -> {
                        currentSites.add(Site(parts[1], parts[2]))
                    }
                }
            }
            flush(result, currentName, currentSites)
            if (result.isEmpty()) FALLBACK_CATEGORIES else result
        } catch (_: Exception) {
            FALLBACK_CATEGORIES
        }
    }

    private fun flush(out: MutableList<Category>, name: String?, sites: MutableList<Site>) {
        if (!name.isNullOrBlank() && sites.isNotEmpty()) {
            out.add(Category(name, sites.toList()))
        }
    }

    private val FALLBACK_CATEGORIES = listOf(
        Category("Anime Streaming", listOf(
            Site("SAnime", "https://sanime-one.vercel.app/"),
            Site("Hianimes", "https://hianimes.se/"),
            Site("Kartoons.me", "https://kartoons.me/home"),
            Site("Netmirror", "http://net77.cc"),
            Site("AnimeWorld", "https://watchanimeworld.top/"),
            Site("AnimeDekho", "https://animedekho.app/home/"),
        )),
        Category("Manga Reading", listOf(
            Site("MangaDex", "https://mangadex.org/"),
            Site("ComicK", "https://comick.dev/"),
        )),
    )
}
