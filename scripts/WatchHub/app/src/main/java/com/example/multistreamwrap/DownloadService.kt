package com.example.multistreamwrap

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import java.io.File
import java.util.Collections
import java.util.concurrent.Executors

data class ActiveDownloadTask(
    val id: String,
    val title: String,
    val streamUrl: String,
    val headers: Map<String, String>,
    var progressPercent: Int = 0,
    var currentSegment: Int = 0,
    var totalSegments: Int = 0,
    var speedFormatted: String = "Connecting...",
    var bytesDownloaded: Long = 0,
    var isCancelled: Boolean = false,
    var downloader: HlsDownloader? = null
)

class DownloadService : Service() {

    companion object {
        private const val TAG = "DownloadService"
        const val CHANNEL_ID = "watchhub_download_channel"
        const val NOTIFICATION_ID = 4001
        private const val MIN_VIDEO_BYTES = 500 * 1024L // 500 KB minimum to be a valid video

        const val ACTION_START = "com.example.multistreamwrap.action.START_DOWNLOAD"
        const val ACTION_CANCEL = "com.example.multistreamwrap.action.CANCEL_DOWNLOAD"
        const val EXTRA_TITLE = "extra_title"
        const val EXTRA_URL = "extra_url"
        const val EXTRA_TASK_ID = "extra_task_id"
        const val EXTRA_HEADERS = "extra_headers"

        private val activeTaskList = Collections.synchronizedList(mutableListOf<ActiveDownloadTask>())
        private val handler = Handler(Looper.getMainLooper())

        var onTaskUpdated: ((ActiveDownloadTask) -> Unit)? = null
        var onTaskFinished: (() -> Unit)? = null

        fun getActiveTasks(): List<ActiveDownloadTask> {
            synchronized(activeTaskList) {
                return activeTaskList.toList()
            }
        }

        fun startDownload(context: Context, title: String, streamUrl: String, headers: Map<String, String>) {
            val intent = Intent(context, DownloadService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_TITLE, title)
                putExtra(EXTRA_URL, streamUrl)
                val bundle = android.os.Bundle()
                for ((k, v) in headers) {
                    bundle.putString(k, v)
                }
                putExtra(EXTRA_HEADERS, bundle)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun cancelTask(taskId: String) {
            val task = activeTaskList.firstOrNull { it.id == taskId }
            if (task != null) {
                task.isCancelled = true
                task.downloader?.isCancelled = true
                activeTaskList.remove(task)
                handler.post {
                    onTaskFinished?.invoke()
                }
            }
        }
    }

    private val executor = Executors.newFixedThreadPool(2)
    private lateinit var notificationManager: NotificationManager

    override fun onCreate() {
        super.onCreate()
        notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action
        if (action == ACTION_CANCEL) {
            val taskId = intent.getStringExtra(EXTRA_TASK_ID)
            if (taskId != null) {
                cancelTask(taskId)
            }
            if (activeTaskList.isEmpty()) {
                try {
                    notificationManager.cancel(NOTIFICATION_ID)
                } catch (_: Exception) {}
                stopForeground(true)
                stopSelf()
            }
            return START_NOT_STICKY
        }

        if (action == ACTION_START) {
            val title = intent.getStringExtra(EXTRA_TITLE) ?: "Video"
            val streamUrl = intent.getStringExtra(EXTRA_URL) ?: return START_NOT_STICKY
            val taskId = System.currentTimeMillis().toString()

            val headersMap = mutableMapOf<String, String>()
            val bundle = intent.getBundleExtra(EXTRA_HEADERS)
            if (bundle != null) {
                for (key in bundle.keySet()) {
                    bundle.getString(key)?.let { headersMap[key] = it }
                }
            }

            val task = ActiveDownloadTask(
                id = taskId,
                title = title,
                streamUrl = streamUrl,
                headers = headersMap,
                speedFormatted = "Connecting..."
            )
            activeTaskList.add(task)

            // Start foreground with initial notification
            startForeground(NOTIFICATION_ID, buildProgressNotification(task))

            // Instantly notify UI so task card appears right away in DownloadsActivity
            handler.post {
                onTaskUpdated?.invoke(task)
            }

            executor.execute {
                runDownloadTask(task)
            }
        }

        return START_STICKY
    }

    private fun runDownloadTask(task: ActiveDownloadTask) {
        val downloader = HlsDownloader(this)
        task.downloader = downloader

        val downloadDir = HlsDownloader.getDownloadDirectory(this)
        val targetFile = HlsDownloader.getUniqueTargetFile(downloadDir, task.title)

        val success = downloader.download(
            streamUrl = task.streamUrl,
            headers = task.headers,
            targetFile = targetFile
        ) { bytesDownloaded, currentSegment, totalSegments, speedBytesPerSec ->
            task.bytesDownloaded = bytesDownloaded
            task.currentSegment = currentSegment
            task.totalSegments = totalSegments
            val percent = if (totalSegments > 0) ((currentSegment * 100) / totalSegments).coerceIn(0, 100) else 0
            task.progressPercent = percent

            val mbPerSec = speedBytesPerSec / (1024.0 * 1024.0)
            task.speedFormatted = if (mbPerSec >= 0.1) {
                String.format("%.1f MB/s", mbPerSec)
            } else {
                "${speedBytesPerSec / 1024} KB/s"
            }

            // Update notification
            try {
                notificationManager.notify(NOTIFICATION_ID, buildProgressNotification(task))
            } catch (_: Exception) {}

            // Notify UI
            handler.post {
                onTaskUpdated?.invoke(task)
            }
        }

        activeTaskList.remove(task)

        if (success && targetFile.exists() && targetFile.length() >= MIN_VIDEO_BYTES) {
            // Save to DownloadStore
            DownloadStore.addRecord(
                this,
                DownloadRecord(
                    id = task.id,
                    title = task.title,
                    filePath = targetFile.absolutePath,
                    fileSize = targetFile.length(),
                    timestamp = System.currentTimeMillis()
                )
            )

            showCompletionNotification(task.title, targetFile)

            // Trigger MediaScanner so video appears immediately in Gallery, VLC, and file managers
            try {
                android.media.MediaScannerConnection.scanFile(
                    this,
                    arrayOf(targetFile.absolutePath),
                    arrayOf("video/mp4"),
                    null
                )
            } catch (_: Exception) {}
        } else if (!task.isCancelled) {
            // Incomplete, corrupt, or failed download - delete partial artifact
            if (targetFile.exists()) {
                try { targetFile.delete() } catch (_: Exception) {}
            }
            showFailureNotification(task.title)
        }

        handler.post {
            onTaskFinished?.invoke()
        }

        if (activeTaskList.isEmpty()) {
            try {
                notificationManager.cancel(NOTIFICATION_ID)
            } catch (_: Exception) {}
            stopForeground(true)
            stopSelf()
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Video Downloads",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Shows progress for ongoing video downloads"
                setShowBadge(false)
            }
            notificationManager.createNotificationChannel(channel)
        }
    }

    private fun buildProgressNotification(task: ActiveDownloadTask): Notification {
        val cancelIntent = Intent(this, DownloadService::class.java).apply {
            action = ACTION_CANCEL
            putExtra(EXTRA_TASK_ID, task.id)
        }
        val cancelPending = PendingIntent.getService(
            this,
            task.id.hashCode(),
            cancelIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
        )

        val openAppIntent = Intent(this, DownloadsActivity::class.java)
        val openAppPending = PendingIntent.getActivity(
            this,
            0,
            openAppIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
        )

        val infoText = if (task.totalSegments > 1) {
            "${task.progressPercent}% • Chunk ${task.currentSegment}/${task.totalSegments} • ${task.speedFormatted}"
        } else {
            "${task.progressPercent}% • ${task.speedFormatted}"
        }

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }

        return builder
            .setContentTitle(task.title)
            .setContentText(infoText)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setProgress(100, task.progressPercent, false)
            .setContentIntent(openAppPending)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Cancel", cancelPending)
            .setOngoing(true)
            .build()
    }

    private fun showCompletionNotification(title: String, file: File) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            try {
                val m = android.os.StrictMode::class.java.getMethod("disableDeathOnFileUriExposure")
                m.invoke(null)
            } catch (_: Exception) {}
        }

        val playIntent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(Uri.fromFile(file), "video/*")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        val playPending = PendingIntent.getActivity(
            this,
            file.hashCode(),
            playIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
        )

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }

        val notif = builder
            .setContentTitle("Download Complete")
            .setContentText(title)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentIntent(playPending)
            .setAutoCancel(true)
            .build()

        notificationManager.notify(file.hashCode(), notif)
    }

    private fun showFailureNotification(title: String) {
        val openAppIntent = Intent(this, DownloadsActivity::class.java)
        val openAppPending = PendingIntent.getActivity(
            this,
            0,
            openAppIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
        )

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }

        val notif = builder
            .setContentTitle("Download Failed")
            .setContentText("Could not download video: $title")
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentIntent(openAppPending)
            .setAutoCancel(true)
            .build()

        notificationManager.notify(title.hashCode(), notif)
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
