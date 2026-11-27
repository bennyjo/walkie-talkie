package dev.walkietalkie;

import android.animation.ValueAnimator;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.RadialGradient;
import android.graphics.Shader;
import android.view.View;
import android.view.animation.LinearInterpolator;

/**
 * The soft clay dot on the call screen, which shows the call's state by how it moves: a slow fade while
 * connecting or switching, a faint breath while listening, a quicker swell while it hears you, and a
 * steady breath while a reply plays. With animations off in the phone's settings, it stands still.
 */
final class Orb extends View {
    static final int WAITING = 0;   // connecting or switching
    static final int LISTENING = 1;
    static final int HEARING = 2;
    static final int REPLYING = 3;

    private static final long CYCLE_MS = 2400;

    private static final float GLOW = 1.7f; // the glow's reach, in core radii

    private final Paint core = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint glow = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RadialGradient glowShade;
    private final Matrix glowPlace = new Matrix();
    private final ValueAnimator clock = ValueAnimator.ofFloat(0f, 1f);
    private int state = WAITING;
    private float phase;
    private float swell; // eases toward the state's size, so a change of state never jumps

    Orb(Context context) {
        super(context);
        int accent = context.getColor(R.color.accent);
        int clear = accent & 0x00FFFFFF;
        core.setColor(accent);
        // A unit-radius glow that fades from the core's edge to nothing, scaled to the core each frame.
        glowShade = new RadialGradient(0, 0, 1, new int[] {accent & 0x66FFFFFF, accent & 0x66FFFFFF, clear},
                new float[] {0f, 1f / GLOW, 1f}, Shader.TileMode.CLAMP);
        glow.setShader(glowShade);
        clock.setDuration(CYCLE_MS);
        clock.setRepeatCount(ValueAnimator.INFINITE);
        clock.setInterpolator(new LinearInterpolator());
        clock.addUpdateListener(a -> {
            phase = (float) a.getAnimatedValue();
            invalidate();
        });
    }

    void setState(int state) {
        this.state = state;
        if (!ValueAnimator.areAnimatorsEnabled()) invalidate();
    }

    @Override protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        if (ValueAnimator.areAnimatorsEnabled()) clock.start();
    }

    @Override protected void onDetachedFromWindow() {
        clock.cancel();
        super.onDetachedFromWindow();
    }

    @Override protected void onVisibilityChanged(View changed, int visibility) {
        super.onVisibilityChanged(changed, visibility);
        if (!ValueAnimator.areAnimatorsEnabled()) return;
        if (isShown()) {
            if (!clock.isStarted()) clock.start();
        } else {
            clock.cancel();
        }
    }

    @Override protected void onDraw(Canvas canvas) {
        float cx = getWidth() / 2f;
        float cy = getHeight() / 2f;
        float r = Math.min(cx, cy) * 0.46f;
        boolean still = !ValueAnimator.areAnimatorsEnabled();
        double wave = still ? 0 : Math.sin(2 * Math.PI * phase * speed());
        float target = state == HEARING ? 0.12f : state == REPLYING ? 0.06f : 0f;
        swell = still ? target : swell + (target - swell) * 0.12f;
        float breath = (float) (amplitude() * wave);
        float size = r * (1f + swell + breath);

        // A soft glow around the core, which reaches further while it hears you or replies.
        float reach = Math.min(size * (GLOW + swell * 2f), Math.min(cx, cy)); // never past the view's edge
        glowPlace.setScale(reach, reach);
        glowPlace.postTranslate(cx, cy);
        glowShade.setLocalMatrix(glowPlace);
        glow.setAlpha(state == WAITING ? 150 : 255);
        canvas.drawCircle(cx, cy, reach, glow);
        core.setAlpha(state == WAITING && !still ? (int) (190 + 60 * wave) : 255);
        canvas.drawCircle(cx, cy, size, core);
    }

    private float amplitude() {
        switch (state) {
            case HEARING:
                return 0.05f;
            case REPLYING:
                return 0.04f;
            case LISTENING:
                return 0.015f;
            default:
                return 0.03f;
        }
    }

    /** Cycles per clock turn: a whole number, so the wave joins up where the clock restarts. */
    private int speed() {
        return state == HEARING ? 3 : state == REPLYING ? 2 : 1;
    }
}
