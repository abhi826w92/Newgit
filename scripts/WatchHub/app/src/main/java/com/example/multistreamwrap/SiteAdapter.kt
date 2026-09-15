package com.example.multistreamwrap

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.ImageView
import android.widget.TextView
import android.net.Uri
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ConcurrentHashMap

class SiteAdapter(
    private val context: Context,
    private val onSiteClick: (Site) -> Unit,
    private val onFavoriteChanged: ((Site) -> Unit)? = null,
    private val onSiteLongClick: ((Site) -> Unit)? = null,
    private val onSiteSwipeDelete: ((Site) -> Unit)? = null
) : BaseAdapter() {

    private var items: List<Any> = emptyList()
    private val faviconCache = ConcurrentHashMap<String, Bitmap>()

    fun submit(newItems: List<Any>) {
        items = newItems
        notifyDataSetChanged()
    }

    override fun getCount(): Int = items.size
    override fun getItem(position: Int): Any = items[position]
    override fun getItemId(position: Int): Long = position.toLong()

    override fun getViewTypeCount(): Int = 2
    override fun getItemViewType(position: Int): Int {
        return if (items[position] is String) 0 else 1
    }
    
    override fun isEnabled(position: Int): Boolean {
        return items[position] !is String
    }

    override fun getView(position: Int, convertView: View?, parent: ViewGroup?): View {
        val item = items[position]
        val type = getItemViewType(position)

        if (type == 0) {
            val view = convertView ?: LayoutInflater.from(context).inflate(context.resources.getIdentifier("item_header", "layout", context.packageName), parent, false)
            val title = view.findViewById<TextView>(context.resources.getIdentifier("header_title", "id", context.packageName))
            title.text = item as String
            return view
        } else {
            val view = convertView ?: LayoutInflater.from(context).inflate(context.resources.getIdentifier("item_site", "layout", context.packageName), parent, false)
            val logo = view.findViewById<ImageView>(context.resources.getIdentifier("site_logo", "id", context.packageName))
            val name = view.findViewById<TextView>(context.resources.getIdentifier("site_name", "id", context.packageName))
            val site = item as Site
            name.text = site.name
            view.tag = site

            val cached = faviconCache[site.domain]
            if (cached != null) {
                logo.setImageBitmap(cached)
            } else {
                logo.setImageResource(context.resources.getIdentifier("ic_placeholder", "drawable", context.packageName))
                loadFavicon(site.domain, logo)
            }

            val lockIcon = view.findViewById<ImageView>(context.resources.getIdentifier("site_lock_icon", "id", context.packageName))
            val isLocked = SiteLockManager.isSiteLocked(context, site.name)
            lockIcon?.visibility = if (isLocked) View.VISIBLE else View.GONE

            val btnFavorite = view.findViewById<ImageView>(context.resources.getIdentifier("btn_favorite", "id", context.packageName))
            
            val isFav = FavoritesManager.isFavorite(context, site.name)
            btnFavorite.setImageResource(
                context.resources.getIdentifier(if (isFav) "ic_heart_filled" else "ic_heart_outline", "drawable", context.packageName)
            )
            
            btnFavorite.setOnClickListener {
                if (FavoritesManager.isFavorite(context, site.name)) {
                    FavoritesManager.removeFavorite(context, site.name)
                    btnFavorite.setImageResource(context.resources.getIdentifier("ic_heart_outline", "drawable", context.packageName))
                } else {
                    FavoritesManager.addFavorite(context, site.name)
                    btnFavorite.setImageResource(context.resources.getIdentifier("ic_heart_filled", "drawable", context.packageName))
                }
                onFavoriteChanged?.invoke(site)
            }

            if (onSiteSwipeDelete != null) {
                view.setOnTouchListener(SwipeItemTouchListener(
                    onSiteClick = { onSiteClick(site) },
                    onSiteSwipeDelete = { onSiteSwipeDelete.invoke(site) }
                ))
            } else {
                view.setOnTouchListener(null)
                view.setOnClickListener { onSiteClick(site) }
                view.setOnLongClickListener {
                    onSiteLongClick?.invoke(site)
                    true
                }
            }
            return view
        }
    }

    private fun loadFavicon(domain: String, imageView: ImageView) {
        Thread {
            var bitmap: Bitmap? = null

            // Source 1: favicon.im service (best quality)
            if (bitmap == null) {
                bitmap = tryDownload("https://favicon.im/$domain")
            }

            // Source 2: Google S2 favicons
            if (bitmap == null) {
                bitmap = tryDownload("https://www.google.com/s2/favicons?domain=$domain&sz=64")
            }

            // Source 3: Direct /favicon.ico
            if (bitmap == null) {
                bitmap = tryDownload("https://$domain/favicon.ico")
            }

            // Source 4: DuckDuckGo favicon
            if (bitmap == null) {
                bitmap = tryDownload("https://icons.duckduckgo.com/ip3/$domain.ico")
            }

            // Source 5: Generate letter icon as final fallback
            if (bitmap == null) {
                bitmap = generateLetterIcon(domain)
            }

            if (bitmap != null) {
                faviconCache[domain] = bitmap
                Handler(Looper.getMainLooper()).post {
                    imageView.setImageBitmap(bitmap)
                }
            }
        }.start()
    }

    private fun tryDownload(urlString: String): Bitmap? {
        return try {
            val conn = URL(urlString).openConnection() as HttpURLConnection
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            conn.instanceFollowRedirects = true
            conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36")
            conn.connect()

            if (conn.responseCode in 200..299) {
                val stream = conn.inputStream
                val bmp = BitmapFactory.decodeStream(stream)
                stream.close()
                conn.disconnect()
                if (bmp != null && bmp.width > 1 && bmp.height > 1) bmp else null
            } else {
                conn.disconnect()
                null
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun generateLetterIcon(domain: String): Bitmap {
        val letter = domain.firstOrNull()?.uppercase() ?: "?"
        val size = 128
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bmp)

        val colors = intArrayOf(
            0xFF7C4DFF.toInt(), 0xFF00BFA5.toInt(), 0xFFFF5252.toInt(),
            0xFF448AFF.toInt(), 0xFFFFAB40.toInt(), 0xFF69F0AE.toInt(),
            0xFFE040FB.toInt(), 0xFF40C4FF.toInt()
        )
        val colorIndex = domain.hashCode().and(0x7FFFFFFF) % colors.size

        val bgPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = colors[colorIndex]
            style = Paint.Style.FILL
        }
        canvas.drawCircle(size / 2f, size / 2f, size / 2f, bgPaint)

        val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = 0xFFFFFFFF.toInt()
            textSize = 56f
            typeface = Typeface.DEFAULT_BOLD
            textAlign = Paint.Align.CENTER
        }
        val y = size / 2f - (textPaint.descent() + textPaint.ascent()) / 2f
        canvas.drawText(letter, size / 2f, y, textPaint)

        return bmp
    }
}
