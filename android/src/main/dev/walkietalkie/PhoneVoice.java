package dev.walkietalkie;

import android.content.Context;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.speech.tts.Voice;
import android.util.Log;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * The phone's own spoken notices, for when the Mac can't hear you or you can't hear its replies. Each is a
 * short fixed sentence from this app, in Android's offline voice, and it plays after the warning tone on
 * whatever output there is, the phone's speaker included. It never says a reply's words, or yours. With no
 * offline English voice on the phone, the warning tone plays alone.
 */
final class PhoneVoice {
    private static final String TAG = "walkie";

    private final SpeakerPipe speaker;
    private final File folder;
    private final TextToSpeech tts;
    private final Map<String, short[]> spoken = new ConcurrentHashMap<>();
    private final Map<String, Boolean> making = new ConcurrentHashMap<>();
    private volatile boolean ready;
    private volatile String wanted; // the sentence to say as soon as it's made

    PhoneVoice(Context context, SpeakerPipe speaker) {
        this.speaker = speaker;
        folder = new File(context.getCacheDir(), "notices");
        tts = new TextToSpeech(context, this::started);
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String id) { }

            @Override public void onDone(String id) {
                made(id);
            }

            @Override public void onError(String id, int code) {
                failed(id);
            }

            @SuppressWarnings("deprecation") // still abstract, so it must be here
            @Override public void onError(String id) {
                failed(id);
            }
        });
    }

    private void started(int status) {
        if (status != TextToSpeech.SUCCESS || !offlineEnglish()) {
            Log.i(TAG, "no offline voice on this phone, so its notices are the tone alone");
            return;
        }
        ready = true;
        String sentence = wanted;
        if (sentence != null) say(sentence);
    }

    /** An English voice that needs no network, so the phone sends nothing anywhere to speak. */
    private boolean offlineEnglish() {
        Voice voice = tts.getVoice();
        if (voice != null && !voice.isNetworkConnectionRequired() && isEnglish(voice)) return true;
        if (tts.getVoices() == null) return false;
        for (Voice candidate : tts.getVoices()) {
            if (!candidate.isNetworkConnectionRequired() && isEnglish(candidate)
                    && tts.setVoice(candidate) == TextToSpeech.SUCCESS) {
                return true;
            }
        }
        return false;
    }

    private static boolean isEnglish(Voice voice) {
        return Locale.ENGLISH.getLanguage().equals(voice.getLocale().getLanguage());
    }

    /** Says one fixed sentence, now if it's been made before, or once it's made. A later sentence replaces it. */
    void say(String sentence) {
        wanted = sentence;
        short[] samples = spoken.get(sentence);
        if (samples != null) {
            wanted = null;
            speaker.say(samples);
            return;
        }
        if (!ready || making.putIfAbsent(sentence, Boolean.TRUE) != null) return;
        if (!folder.isDirectory() && !folder.mkdirs()) return;
        int result = tts.synthesizeToFile(sentence, new Bundle(), file(sentence), sentence);
        if (result != TextToSpeech.SUCCESS) making.remove(sentence);
    }

    /** What went wrong is fixed, so a sentence still being made isn't said. */
    void cancel() {
        wanted = null;
    }

    void close() {
        tts.shutdown();
    }

    private void failed(String sentence) {
        making.remove(sentence);
        Log.i(TAG, "the phone's voice couldn't make a notice");
        file(sentence).delete(); // whatever part of it was written
    }

    private void made(String sentence) {
        File file = file(sentence);
        try {
            short[] samples = Wav.toSpeaker(Files.readAllBytes(file.toPath()));
            if (samples != null) spoken.put(sentence, samples);
        } catch (IOException e) {
            Log.i(TAG, "couldn't read a notice the phone's voice made");
        } finally {
            making.remove(sentence);
            if (!file.delete()) Log.i(TAG, "couldn't delete a notice file");
        }
        String now = wanted;
        if (sentence.equals(now)) say(now);
    }

    private File file(String sentence) {
        return new File(folder, Integer.toHexString(sentence.hashCode()) + ".wav");
    }
}
