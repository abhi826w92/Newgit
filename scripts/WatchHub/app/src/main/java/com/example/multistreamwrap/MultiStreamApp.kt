package com.example.multistreamwrap

import android.app.Application

class MultiStreamApp : Application() {

    override fun onCreate() {
        super.onCreate()
        // Load the host list once (singleton) to avoid reloading it in every WebActivity.
        AdBlockStore.get(this)
    }
}
