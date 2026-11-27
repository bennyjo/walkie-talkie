package dev.walkietalkie;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * The app's look, in one place: the type scale, spacing, corners, and the few views the screens are built
 * from. The colours are tokens in res/values/colors.xml, with the dark set under the same names in
 * res/values-night, so every view here follows the system's light or dark mode.
 *
 * The type pairs a serif for headings, as a calm, bookish voice, with the system sans for everything you
 * read or tap. Both are the phone's own fonts.
 */
final class Ui {
    // Spacing, in dp. Screens use a 24 dp gutter.
    static final int SPACE_XS = 4;
    static final int SPACE_S = 8;
    static final int SPACE_M = 12;
    static final int SPACE_L = 16;
    static final int SPACE_XL = 24;
    static final int SPACE_XXL = 32;

    // Corners, in dp. Buttons are full pills.
    static final int RADIUS_CARD = 20;
    static final int RADIUS_FIELD = 14;
    static final int RADIUS_NOTE = 14;
    static final int BUTTON_HEIGHT = 52;

    static final Typeface SERIF = Typeface.create("serif", Typeface.NORMAL);
    static final Typeface SANS = Typeface.create("sans-serif", Typeface.NORMAL);
    static final Typeface SANS_MEDIUM = Typeface.create("sans-serif-medium", Typeface.NORMAL);

    private final Context context;

    Ui(Context context) {
        this.context = context;
    }

    int dp(float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                context.getResources().getDisplayMetrics()));
    }

    int color(int id) {
        return context.getColor(id);
    }

    // --- type ----------------------------------------------------------------------

    /** A screen's headline: what the screen is for. */
    TextView display(CharSequence value) {
        TextView view = text(value, SERIF, 32, R.color.text);
        view.setLineSpacing(0, 1.1f);
        return view;
    }

    /** A heading inside a screen, such as the state under the dot. */
    TextView title(CharSequence value) {
        return text(value, SERIF, 22, R.color.text);
    }

    TextView body(CharSequence value) {
        TextView view = text(value, SANS, 16, R.color.text);
        view.setLineSpacing(0, 1.25f);
        return view;
    }

    TextView muted(CharSequence value) {
        TextView view = text(value, SANS, 15, R.color.text_muted);
        view.setLineSpacing(0, 1.25f);
        return view;
    }

    /** A small label over a group, such as an account's name. */
    TextView label(CharSequence value) {
        TextView view = text(value, SANS_MEDIUM, 13, R.color.text_muted);
        view.setLetterSpacing(0.04f);
        return view;
    }

    TextView text(CharSequence value, Typeface face, int sp, int colour) {
        TextView view = new TextView(context);
        view.setText(value);
        view.setTypeface(face);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        view.setTextColor(color(colour));
        view.setIncludeFontPadding(true);
        return view;
    }

    // --- surfaces ------------------------------------------------------------------

    GradientDrawable rounded(int fill, int radiusDp, int stroke) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(fill);
        shape.setCornerRadius(dp(radiusDp));
        if (stroke != 0) shape.setStroke(dp(1), stroke);
        return shape;
    }

    /** A pressed state that ripples inside the shape. */
    Drawable pressable(GradientDrawable shape, int rippleColour) {
        GradientDrawable mask = new GradientDrawable();
        mask.setColor(0xFFFFFFFF);
        mask.setCornerRadius(shape.getCornerRadius());
        return new RippleDrawable(ColorStateList.valueOf(rippleColour), shape, mask);
    }

    /** A white card with a hairline outline, padded inside. */
    LinearLayout card() {
        LinearLayout card = new LinearLayout(context);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackground(rounded(color(R.color.surface), RADIUS_CARD, color(R.color.outline)));
        card.setPadding(dp(SPACE_L + 4), dp(SPACE_L), dp(SPACE_L + 4), dp(SPACE_L));
        return card;
    }

    /** A card that is itself the tap target. */
    LinearLayout tappableCard(Runnable action) {
        LinearLayout card = card();
        card.setBackground(pressable(rounded(color(R.color.surface), RADIUS_CARD, color(R.color.outline)),
                color(R.color.surface_muted)));
        card.setClickable(true);
        card.setFocusable(true);
        card.setOnClickListener(v -> action.run());
        return card;
    }

    /** A short message in a soft box: plain, or in the danger colours for something that went wrong. */
    TextView note(boolean danger) {
        TextView view = text("", SANS, 15, R.color.text);
        view.setLineSpacing(0, 1.2f);
        view.setPadding(dp(SPACE_L), dp(SPACE_M), dp(SPACE_L), dp(SPACE_M));
        noteStyle(view, danger);
        return view;
    }

    /** Turns a note plain or danger. Does nothing when it already is. */
    void noteStyle(TextView view, boolean danger) {
        if (Boolean.valueOf(danger).equals(view.getTag())) return;
        view.setTag(danger);
        view.setTextColor(color(danger ? R.color.danger : R.color.text));
        view.setBackground(rounded(color(danger ? R.color.danger_soft : R.color.surface_muted), RADIUS_NOTE, 0));
    }

    // --- controls ------------------------------------------------------------------

    /** The one filled button a screen has: its main action. */
    Button primary(String value, Runnable action) {
        Button view = baseButton(value, action);
        view.setTextColor(color(R.color.on_accent));
        view.setBackground(pressable(rounded(color(R.color.accent), BUTTON_HEIGHT / 2, 0), 0x33FFFFFF));
        return view;
    }

    /** An outlined pill, for a second choice beside the primary one. */
    Button secondary(String value, Runnable action) {
        Button view = baseButton(value, action);
        view.setTextColor(color(R.color.text));
        view.setBackground(pressable(rounded(0, BUTTON_HEIGHT / 2, color(R.color.outline)), color(R.color.surface_muted)));
        return view;
    }

    /** A text button, for quiet actions such as Cancel or Pair again. */
    Button quiet(String value, Runnable action) {
        Button view = baseButton(value, action);
        view.setTextColor(color(R.color.accent_text));
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        view.setBackground(pressable(rounded(0, BUTTON_HEIGHT / 2, 0), color(R.color.surface_muted)));
        view.setPadding(dp(SPACE_L), 0, dp(SPACE_L), 0);
        return view;
    }

    private Button baseButton(String value, Runnable action) {
        Button view = new Button(context);
        view.setText(value);
        view.setAllCaps(false);
        view.setTypeface(SANS_MEDIUM);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        view.setStateListAnimator(null);
        view.setMinHeight(dp(BUTTON_HEIGHT));
        view.setMinimumHeight(dp(BUTTON_HEIGHT));
        view.setGravity(Gravity.CENTER);
        view.setSingleLine(true);
        view.setEllipsize(TextUtils.TruncateAt.END);
        view.setOnClickListener(v -> action.run());
        return view;
    }

    EditText field(String hint, int inputType) {
        EditText view = new EditText(context);
        view.setHint(hint);
        view.setInputType(inputType);
        view.setSingleLine(true);
        view.setTypeface(SANS);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
        view.setTextColor(color(R.color.text));
        view.setHintTextColor(color(R.color.text_muted));
        view.setBackground(rounded(color(R.color.surface_muted), RADIUS_FIELD, 0));
        view.setPadding(dp(SPACE_L), dp(SPACE_M + 2), dp(SPACE_L), dp(SPACE_M + 2));
        return view;
    }

    /** A full-width row's layout parameters, with a top margin. */
    LinearLayout.LayoutParams below(int topDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        params.topMargin = dp(topDp);
        return params;
    }

    static void shown(View view, boolean shown) {
        int visibility = shown ? View.VISIBLE : View.GONE;
        if (view.getVisibility() != visibility) view.setVisibility(visibility);
    }
}
