package com.example.multistreamwrap

import android.content.Context

/**
 * Build worker me generated R class compile nahi hoti, isliye resources ko
 * naam ke basis par runtime par lookup karte hain — pure framework API, koi dep nahi.
 */
fun Context.layout(name: String): Int =
    resources.getIdentifier(name, "layout", packageName)

fun Context.resId(name: String): Int =
    resources.getIdentifier(name, "id", packageName)

fun Context.stringRes(name: String): Int =
    resources.getIdentifier(name, "string", packageName)

fun Context.drawableRes(name: String): Int =
    resources.getIdentifier(name, "drawable", packageName)

fun Context.colorRes(name: String): Int =
    resources.getIdentifier(name, "color", packageName)
