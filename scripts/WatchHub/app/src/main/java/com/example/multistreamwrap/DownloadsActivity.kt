package com.example.multistreamwrap

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import java.io.File

class DownloadsActivity : Activity() {

    private lateinit var listDownloads: ListView
    private lateinit var emptyView: TextView
    private lateinit var layoutActiveContainer: LinearLayout
    private lateinit var layoutActiveList: LinearLayout

    private var completedList = mutableListOf<DownloadRecord>()
    private lateinit var adapter: DownloadsAdapter

    override fun attachBaseContext(newBase: Context) {
        super.attachBaseContext(ThemeManager.wrapContext(newBase))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        ThemeManager.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(layout("activity_downloads"))
        fixStatusBar()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            try {
                val m = android.os.StrictMode::class.java.getMethod("disableDeathOnFileUriExposure")
                m.invoke(null)
            } catch (_: Exception) {}
        }

        val root = findViewById<View>(resId("root_layout"))
        val toolbar = findViewById<LinearLayout>(resId("toolbar"))
        root.setOnApplyWindowInsetsListener { _, insets ->
            val top = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                insets.getInsets(android.view.WindowInsets.Type.systemBars()).top
            } else {
                @Suppress("DEPRECATION")
                insets.systemWindowInsetTop
            }
            val bottom = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                insets.getInsets(android.view.WindowInsets.Type.systemBars()).bottom
            } else {
                @Suppress("DEPRECATION")
                insets.systemWindowInsetBottom
            }
            val left = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                insets.getInsets(android.view.WindowInsets.Type.systemBars()).left
            } else {
                @Suppress("DEPRECATION")
                insets.systemWindowInsetLeft
            }
            val right = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                insets.getInsets(android.view.WindowInsets.Type.systemBars()).right
            } else {
                @Suppress("DEPRECATION")
                insets.systemWindowInsetRight
            }

            toolbar.setPadding(toolbar.paddingLeft, top, toolbar.paddingRight, toolbar.paddingBottom)
            root.setPadding(left, 0, right, bottom)
            insets
        }

        findViewById<ImageButton>(resId("btn_back")).setOnClickListener {
            finish()
        }

        listDownloads = findViewById(resId("list_downloads"))
        emptyView = findViewById(resId("empty_view"))
        layoutActiveContainer = findViewById(resId("layout_active_container"))
        layoutActiveList = findViewById(resId("layout_active_list"))

        adapter = DownloadsAdapter()
        listDownloads.adapter = adapter

        refreshData()

        DownloadService.onTaskUpdated = {
            runOnUiThread {
                updateActiveDownloads()
            }
        }

        DownloadService.onTaskFinished = {
            runOnUiThread {
                refreshData()
            }
        }
    }

    override fun onResume() {
        super.onResume()
        refreshData()
    }

    override fun onDestroy() {
        super.onDestroy()
        DownloadService.onTaskUpdated = null
        DownloadService.onTaskFinished = null
    }

    private fun fixStatusBar() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            window.decorView.systemUiVisibility =
                window.decorView.systemUiVisibility and View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR.inv()
        }
    }

    private fun refreshData() {
        updateActiveDownloads()
        completedList.clear()
        completedList.addAll(DownloadStore.getRecords(this))
        adapter.notifyDataSetChanged()

        val hasActive = DownloadService.getActiveTasks().isNotEmpty()
        val hasCompleted = completedList.isNotEmpty()

        emptyView.visibility = if (!hasActive && !hasCompleted) View.VISIBLE else View.GONE
    }

    private fun updateActiveDownloads() {
        val tasks = DownloadService.getActiveTasks()
        if (tasks.isEmpty()) {
            layoutActiveContainer.visibility = View.GONE
            layoutActiveList.removeAllViews()
            return
        }

        layoutActiveContainer.visibility = View.VISIBLE
        layoutActiveList.removeAllViews()

        val inflater = LayoutInflater.from(this)
        for (task in tasks) {
            val itemView = inflater.inflate(layout("item_download_active"), layoutActiveList, false)
            val tvTitle = itemView.findViewById<TextView>(resId("tv_active_title"))
            val tvStatus = itemView.findViewById<TextView>(resId("tv_active_status"))
            val tvPercent = itemView.findViewById<TextView>(resId("tv_active_percent"))
            val pbProgress = itemView.findViewById<ProgressBar>(resId("pb_active_progress"))
            val btnCancel = itemView.findViewById<ImageButton>(resId("btn_cancel_active"))

            tvTitle.text = task.title
            tvPercent.text = "${task.progressPercent}%"
            pbProgress.progress = task.progressPercent

            val statusText = if (task.totalSegments > 1) {
                "Chunk ${task.currentSegment}/${task.totalSegments} • ${task.speedFormatted}"
            } else {
                task.speedFormatted
            }
            tvStatus.text = statusText

            btnCancel.setOnClickListener {
                DownloadService.cancelTask(task.id)
                Toast.makeText(this, "Cancelling download...", Toast.LENGTH_SHORT).show()
                refreshData()
            }

            layoutActiveList.addView(itemView)
        }
    }

    private fun playVideo(record: DownloadRecord) {
        val file = File(record.filePath)
        if (!file.exists()) {
            Toast.makeText(this, "File not found on device", Toast.LENGTH_SHORT).show()
            DownloadStore.deleteRecord(this, record.id)
            refreshData()
            return
        }

        try {
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(Uri.fromFile(file), "video/*")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            startActivity(Intent.createChooser(intent, "Play Video With"))
        } catch (e: Exception) {
            Toast.makeText(this, "No video player found", Toast.LENGTH_SHORT).show()
        }
    }

    private fun shareVideo(record: DownloadRecord) {
        val file = File(record.filePath)
        if (!file.exists()) {
            Toast.makeText(this, "File not found", Toast.LENGTH_SHORT).show()
            return
        }

        try {
            val intent = Intent(Intent.ACTION_SEND).apply {
                type = "video/*"
                putExtra(Intent.EXTRA_STREAM, Uri.fromFile(file))
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            startActivity(Intent.createChooser(intent, "Share Video"))
        } catch (e: Exception) {
            Toast.makeText(this, "Failed to share video", Toast.LENGTH_SHORT).show()
        }
    }

    private fun confirmDelete(record: DownloadRecord) {
        AlertDialog.Builder(this)
            .setTitle("Delete Video")
            .setMessage("Are you sure you want to delete \"${record.title}\"?")
            .setPositiveButton("Delete") { _, _ ->
                DownloadStore.deleteRecord(this, record.id)
                Toast.makeText(this, "Video deleted", Toast.LENGTH_SHORT).show()
                refreshData()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private inner class DownloadsAdapter : BaseAdapter() {
        override fun getCount(): Int = completedList.size
        override fun getItem(position: Int): Any = completedList[position]
        override fun getItemId(position: Int): Long = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup?): View {
            val view = convertView ?: LayoutInflater.from(this@DownloadsActivity)
                .inflate(layout("item_download"), parent, false)

            val record = completedList[position]
            val tvTitle = view.findViewById<TextView>(resId("tv_download_title"))
            val tvInfo = view.findViewById<TextView>(resId("tv_download_info"))
            val btnShare = view.findViewById<ImageButton>(resId("btn_share_download"))
            val btnDelete = view.findViewById<ImageButton>(resId("btn_delete_download"))

            tvTitle.text = record.title
            tvInfo.text = "${record.formattedSize} • ${record.formattedDate}"

            view.setOnClickListener {
                playVideo(record)
            }

            btnShare.setOnClickListener {
                shareVideo(record)
            }

            btnDelete.setOnClickListener {
                confirmDelete(record)
            }

            return view
        }
    }
}
