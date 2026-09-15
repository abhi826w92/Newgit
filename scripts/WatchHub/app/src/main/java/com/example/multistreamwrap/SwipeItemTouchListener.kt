package com.example.multistreamwrap

import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration

class SwipeItemTouchListener(
    private val onSiteClick: (Site) -> Unit,
    private val onSiteSwipeDelete: (Site) -> Unit
) : View.OnTouchListener {

    private var downX = 0f
    private var downY = 0f
    private var isSwiping = false
    private var touchSlop = 0

    override fun onTouch(v: View, event: MotionEvent): Boolean {
        if (touchSlop == 0) {
            touchSlop = ViewConfiguration.get(v.context).scaledTouchSlop
        }

        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = event.rawX
                downY = event.rawY
                isSwiping = false
                v.animate().cancel()
                return true
            }

            MotionEvent.ACTION_MOVE -> {
                val deltaX = event.rawX - downX
                val deltaY = event.rawY - downY

                if (!isSwiping && Math.abs(deltaX) > touchSlop && Math.abs(deltaX) > Math.abs(deltaY)) {
                    isSwiping = true
                    v.parent?.requestDisallowInterceptTouchEvent(true)
                }

                if (isSwiping) {
                    v.translationX = deltaX
                    v.alpha = Math.max(0.2f, 1f - (Math.abs(deltaX) / v.width))
                    return true
                }
            }

            MotionEvent.ACTION_UP -> {
                val deltaX = event.rawX - downX
                val deltaY = event.rawY - downY

                if (isSwiping) {
                    val width = v.width
                    if (Math.abs(deltaX) > width / 3) {
                        val targetX = if (deltaX > 0) width.toFloat() else -width.toFloat()
                        v.animate()
                            .translationX(targetX)
                            .alpha(0f)
                            .setDuration(200)
                            .withEndAction {
                                v.translationX = 0f
                                v.alpha = 1f
                                val site = v.tag as? Site
                                if (site != null) {
                                    onSiteSwipeDelete(site)
                                }
                            }
                            .start()
                    } else {
                        v.animate()
                            .translationX(0f)
                            .alpha(1f)
                            .setDuration(200)
                            .start()
                    }
                    return true
                } else if (Math.abs(deltaX) < touchSlop && Math.abs(deltaY) < touchSlop) {
                    v.performClick()
                    val site = v.tag as? Site
                    if (site != null) {
                        onSiteClick(site)
                    }
                    return true
                }
            }

            MotionEvent.ACTION_CANCEL -> {
                if (isSwiping) {
                    v.animate()
                        .translationX(0f)
                        .alpha(1f)
                        .setDuration(200)
                        .start()
                    isSwiping = false
                    return true
                }
            }
        }
        return false
    }
}
