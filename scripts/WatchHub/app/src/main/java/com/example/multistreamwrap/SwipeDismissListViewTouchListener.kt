package com.example.multistreamwrap

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.graphics.Rect
import android.view.MotionEvent
import android.view.VelocityTracker
import android.view.View
import android.view.ViewConfiguration
import android.widget.ListView

class SwipeDismissListViewTouchListener(
    private val listView: ListView,
    private val callbacks: DismissCallbacks
) : View.OnTouchListener {

    interface DismissCallbacks {
        fun canDismiss(position: Int): Boolean
        fun onDismiss(listView: ListView, position: Int)
    }

    private val slop: Int
    private val minFlingVelocity: Int
    private val maxFlingVelocity: Int
    private val animationTime: Long

    private var downX = 0f
    private var downY = 0f
    private var swiping = false
    private var swipingSlop = 0
    private var velocityTracker: VelocityTracker? = null
    private var downPosition = ListView.INVALID_POSITION
    private var downView: View? = null
    private var paused = false

    init {
        val vc = ViewConfiguration.get(listView.context)
        slop = vc.scaledTouchSlop
        minFlingVelocity = vc.scaledMinimumFlingVelocity * 16
        maxFlingVelocity = vc.scaledMaximumFlingVelocity
        animationTime = listView.context.resources.getInteger(android.R.integer.config_shortAnimTime).toLong()
    }

    override fun onTouch(view: View, motionEvent: MotionEvent): Boolean {
        when (motionEvent.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                if (paused) return false
                val rect = Rect()
                val childCount = listView.childCount
                val listViewCoords = IntArray(2)
                listView.getLocationOnScreen(listViewCoords)
                val x = motionEvent.rawX.toInt() - listViewCoords[0]
                val y = motionEvent.rawY.toInt() - listViewCoords[1]
                var child: View? = null
                for (i in 0 until childCount) {
                    val c = listView.getChildAt(i)
                    c.getHitRect(rect)
                    if (rect.contains(x, y)) {
                        child = c
                        break
                    }
                }

                if (child != null) {
                    downX = motionEvent.rawX
                    downY = motionEvent.rawY
                    downPosition = listView.getPositionForView(child)
                    if (callbacks.canDismiss(downPosition)) {
                        velocityTracker = VelocityTracker.obtain()
                        velocityTracker?.addMovement(motionEvent)
                        downView = child
                    } else {
                        downView = null
                    }
                }
                return false
            }

            MotionEvent.ACTION_CANCEL -> {
                if (velocityTracker == null) return false
                downView?.animate()
                    ?.translationX(0f)
                    ?.alpha(1f)
                    ?.setDuration(animationTime)
                    ?.setListener(null)
                velocityTracker?.recycle()
                velocityTracker = null
                downX = 0f
                downY = 0f
                downView = null
                downPosition = ListView.INVALID_POSITION
                swiping = false
            }

            MotionEvent.ACTION_UP -> {
                if (velocityTracker == null) return false
                val deltaX = motionEvent.rawX - downX
                velocityTracker?.addMovement(motionEvent)
                velocityTracker?.computeCurrentVelocity(1000)
                val velocityX = velocityTracker?.xVelocity ?: 0f
                val absVelocityX = Math.abs(velocityX)
                val absVelocityY = Math.abs(velocityTracker?.yVelocity ?: 0f)
                var dismiss = false
                var dismissRight = false
                if (Math.abs(deltaX) > listView.width / 3 && swiping) {
                    dismiss = true
                    dismissRight = deltaX > 0
                } else if (minFlingVelocity <= absVelocityX && absVelocityX <= maxFlingVelocity
                    && absVelocityY < absVelocityX && swiping) {
                    dismiss = (velocityX < 0) == (deltaX < 0)
                    dismissRight = (velocityTracker?.xVelocity ?: 0f) > 0
                }
                if (dismiss && downPosition != ListView.INVALID_POSITION) {
                    val dismissView = downView
                    val dismissPosition = downPosition
                    dismissView?.animate()
                        ?.translationX(if (dismissRight) listView.width.toFloat() else -listView.width.toFloat())
                        ?.alpha(0f)
                        ?.setDuration(animationTime)
                        ?.setListener(object : AnimatorListenerAdapter() {
                            override fun onAnimationEnd(animation: Animator) {
                                dismissView.translationX = 0f
                                dismissView.alpha = 1f
                                callbacks.onDismiss(listView, dismissPosition)
                            }
                        })
                } else {
                    downView?.animate()
                        ?.translationX(0f)
                        ?.alpha(1f)
                        ?.setDuration(animationTime)
                        ?.setListener(null)
                }
                velocityTracker?.recycle()
                velocityTracker = null
                downX = 0f
                downY = 0f
                downView = null
                downPosition = ListView.INVALID_POSITION
                swiping = false
            }

            MotionEvent.ACTION_MOVE -> {
                if (velocityTracker == null || paused) return false
                velocityTracker?.addMovement(motionEvent)
                val deltaX = motionEvent.rawX - downX
                val deltaY = motionEvent.rawY - downY
                if (Math.abs(deltaX) > slop && Math.abs(deltaY) < Math.abs(deltaX) / 2) {
                    swiping = true
                    swipingSlop = if (deltaX > 0) slop else -slop
                    listView.requestDisallowInterceptTouchEvent(true)

                    val cancelEvent = MotionEvent.obtain(motionEvent)
                    cancelEvent.action = MotionEvent.ACTION_CANCEL or (motionEvent.actionIndex shl MotionEvent.ACTION_POINTER_INDEX_SHIFT)
                    listView.onTouchEvent(cancelEvent)
                    cancelEvent.recycle()
                }

                if (swiping) {
                    downView?.translationX = deltaX - swipingSlop
                    downView?.alpha = Math.max(0.1f, Math.min(1f, 1f - 2f * Math.abs(deltaX) / listView.width))
                    return true
                }
            }
        }
        return false
    }
}
