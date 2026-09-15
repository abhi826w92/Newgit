package com.example.multistreamwrap

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

object DialogLoader {

    private const val DEFAULT_DIALOG_ID = "-OzZMwS7u_Jag5YNaHyy"
    private val DATABASE_BASE_URL: String by lazy { firebaseBaseUrl() }

    private fun firebaseBaseUrl(): String {
        val native = NativeLib.getFirebaseUrl()
        return if (native.isBlank() || native == "ERROR_LIB_NOT_LOADED") {
            "https://pymob-3bfb1-default-rtdb.firebaseio.com/dialogs/"
        } else {
            native
        }
    }

    private fun getDialogId(placementTag: String): String {
        return DEFAULT_DIALOG_ID
    }

    private fun getFirebaseUrl(dialogId: String): String {
        return DATABASE_BASE_URL + dialogId + ".json"
    }

    private fun getClickTrackingUrl(dialogId: String): String {
        return DATABASE_BASE_URL + dialogId + "/stats/clicks.json"
    }

    private fun getImpressionTrackingUrl(dialogId: String): String {
        return DATABASE_BASE_URL + dialogId + "/stats/impressions.json"
    }

    private var currentDialog: AlertDialog? = null
    private var lastJsonHash = ""
    private var isMonitoring = false

    fun startMonitoring(context: Context, placementTag: String = "default") {
        if (isMonitoring) return
        isMonitoring = true

        Thread {
            while (isMonitoring) {
                checkForUpdate(context, placementTag)
                try {
                    Thread.sleep(30000)
                } catch (e: InterruptedException) {
                    e.printStackTrace()
                }
            }
        }.start()
    }

    fun show(context: Context, placementTag: String = "default") {
        Thread {
            checkForUpdate(context, placementTag)
        }.start()
    }

    private fun checkForUpdate(context: Context, placementTag: String) {
        try {
            val dialogId = getDialogId(placementTag)
            Log.d("DialogLoader", "Checking update for placement: $placementTag (ID: $dialogId)...")
            val url = URL(getFirebaseUrl(dialogId))
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "GET"
            conn.connectTimeout = 5000
            conn.readTimeout = 5000
            conn.useCaches = false
            conn.defaultUseCaches = false
            conn.setRequestProperty("Cache-Control", "no-cache")
            conn.setRequestProperty("Pragma", "no-cache")

            val responseCode = conn.responseCode
            Log.d("DialogLoader", "Response Code: $responseCode")

            if (responseCode == HttpURLConnection.HTTP_OK) {
                val reader = BufferedReader(InputStreamReader(conn.inputStream))
                val content = StringBuilder()
                var inputLine: String?
                while (reader.readLine().also { inputLine = it } != null) {
                    content.append(inputLine)
                }
                reader.close()
                conn.disconnect()

                val jsonResponse = content.toString()

                if (jsonResponse == lastJsonHash) {
                    Log.d("DialogLoader", "No change in dialog config.")
                    return
                }

                Log.d("DialogLoader", "New JSON Content detected. Re-evaluating.")

                if (jsonResponse == "null") {
                    Log.d("DialogLoader", "No dialog data found (or dialog deleted).")
                    lastJsonHash = ""
                    dismissCurrentDialog()
                    return
                }

                val jsonObject = JSONObject(jsonResponse)
                val title = jsonObject.optString("title", "Notice")
                val message = jsonObject.optString("message", "Message")
                val btn1Text = jsonObject.optString("btn1Text", "OK")
                val btn1Link = jsonObject.optString("btn1Link", "")
                val btn2Text = jsonObject.optString("btn2Text", "")
                val btn2Link = jsonObject.optString("btn2Link", "")
                val cancelAction = jsonObject.optString("cancelAction", "")
                val expiryDateStr = jsonObject.optString("expiryDate", "")
                val targetVersion = jsonObject.optInt("targetVersion", -1)
                val isMaintenance = jsonObject.optBoolean("isMaintenance", false)

                if (targetVersion > 0) {
                    try {
                        val currentVersion = context.packageManager.getPackageInfo(context.packageName, 0).versionCode
                        Log.d("DialogLoader", "Version Check: Current=$currentVersion, Target=$targetVersion")
                        if (currentVersion >= targetVersion) {
                            Log.d("DialogLoader", "App is up-to-date. Skipping dialog.")
                            return
                        }
                    } catch (e: Exception) {
                        e.printStackTrace()
                    }
                } else {
                    Log.d("DialogLoader", "No target version set. Showing to all.")
                }

                if (expiryDateStr.isNotEmpty()) {
                    try {
                        val sdf = SimpleDateFormat("yyyy-MM-dd'T'HH:mm", Locale.getDefault())
                        val expiryDate = sdf.parse(expiryDateStr)
                        if (expiryDate != null && Date().after(expiryDate)) {
                            return
                        }
                    } catch (ignored: Exception) {}
                }

                Handler(Looper.getMainLooper()).post {
                    dismissCurrentDialog()

                    val builder = AlertDialog.Builder(context)
                    builder.setTitle(title)
                    builder.setMessage(message)
                    builder.setCancelable(false)

                    if (isMaintenance) {
                        builder.setPositiveButton("Exit") { _, _ ->
                            if (context is Activity) {
                                context.finishAffinity()
                            }
                            System.exit(0)
                        }
                    } else {
                        builder.setPositiveButton(btn1Text) { _, _ ->
                            trackClick(dialogId)
                            if (btn1Link.isNotEmpty()) {
                                val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(btn1Link))
                                browserIntent.flags = Intent.FLAG_ACTIVITY_NEW_TASK
                                context.startActivity(browserIntent)
                            }
                        }

                        if (btn2Text.isNotEmpty()) {
                            builder.setNegativeButton(btn2Text) { _, _ ->
                                if (cancelAction.equals("exit", ignoreCase = true)) {
                                    if (context is Activity) {
                                        context.finishAffinity()
                                    }
                                    System.exit(0)
                                } else if (btn2Link.isNotEmpty()) {
                                    val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(btn2Link))
                                    browserIntent.flags = Intent.FLAG_ACTIVITY_NEW_TASK
                                    context.startActivity(browserIntent)
                                }
                            }
                        }
                    }

                    try {
                        currentDialog = builder.show()
                        lastJsonHash = jsonResponse
                        trackImpression(dialogId)
                    } catch (e: Exception) {
                        e.printStackTrace()
                    }
                }
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private fun dismissCurrentDialog() {
        if (currentDialog?.isShowing == true) {
            try {
                currentDialog?.dismiss()
            } catch (ignored: Exception) {}
        }
        currentDialog = null
    }

    private fun trackClick(dialogId: String) {
        Thread {
            try {
                val url = URL(getClickTrackingUrl(dialogId))
                val conn = url.openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")

                val device = android.os.Build.MANUFACTURER.replace("\"", "\\\"") + " " + android.os.Build.MODEL.replace("\"", "\\\"")
                val os = android.os.Build.VERSION.RELEASE.replace("\"", "\\\"")
                val payload = "{" +
                        "\"timestamp\":" + System.currentTimeMillis() + "," +
                        "\"deviceModel\":\"" + device + "\"," +
                        "\"osVersion\":\"" + os + "\"" +
                        "}"

                conn.outputStream.write(payload.toByteArray(Charsets.UTF_8))
                conn.responseCode
                conn.disconnect()
            } catch (ignored: Exception) {}
        }.start()
    }

    private fun trackImpression(dialogId: String) {
        Thread {
            try {
                val url = URL(getImpressionTrackingUrl(dialogId))
                val conn = url.openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")

                val device = android.os.Build.MANUFACTURER.replace("\"", "\\\"") + " " + android.os.Build.MODEL.replace("\"", "\\\"")
                val os = android.os.Build.VERSION.RELEASE.replace("\"", "\\\"")
                val payload = "{" +
                        "\"timestamp\":" + System.currentTimeMillis() + "," +
                        "\"deviceModel\":\"" + device + "\"," +
                        "\"osVersion\":\"" + os + "\"" +
                        "}"

                conn.outputStream.write(payload.toByteArray(Charsets.UTF_8))
                conn.responseCode
                conn.disconnect()
            } catch (ignored: Exception) {}
        }.start()
    }
}
