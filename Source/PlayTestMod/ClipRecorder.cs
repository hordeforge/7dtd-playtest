using System;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// On-demand in-game clip recorder: capture a sampled frame sequence of
    /// whatever is actually happening, not only a staged hold.
    ///
    /// <para><see cref="CaseDef.StagedClip"/> captures frames while a staged
    /// scene holds still (or turns). This recorder decouples the same
    /// guarantee from staging: any case (<see cref="CaseDef.Live"/> included)
    /// starts it with <see cref="Helpers.BeginClip"/>, does whatever needs
    /// recording (walk a worn garment, fire a VFX, use an item), and stops it
    /// with <see cref="Helpers.EndClip"/>. Frames land in
    /// <c>playtest-shots/clips/&lt;id&gt;/frame-XXXX.png</c> via
    /// <see cref="Helpers.CaptureClipFrame"/>, so the same
    /// <c>clip complete</c> completion line and
    /// <c>scripts/capture_video.sh</c> muxing work unchanged.</para>
    ///
    /// <para>A clip abandoned by a failed or timed-out case must not read as a
    /// completed one. The runner aborts any still-active clip once it
    /// finishes, and a hard cap abandons a clip nobody stopped; both emit
    /// <c>clip abandoned</c>, which is deliberately not the completion
    /// marker, so a collector never muxes a partial clip as if it were
    /// complete.</para>
    /// </summary>
    public static class ClipRecorder
    {
        const float MaxClipSeconds = 300f;

        static string _activeId;
        static int _superSize = 2;
        static float _interval = 0.25f; // 1 / default fps
        static float _nextFrameAt;
        static float _startedAt;
        static int _frames;

        /// <summary>Whether a clip is currently recording.</summary>
        public static bool Active => _activeId != null;

        /// <summary>Start recording <paramref name="id"/> at the chosen cadence.</summary>
        public static void Begin(string id, int superSize, float fps)
        {
            if (string.IsNullOrEmpty(id))
                throw new ArgumentException("ClipRecorder.Begin needs a clip id");
            if (superSize < 1) superSize = 1;
            if (!(fps > 0f))
                throw new ArgumentOutOfRangeException(nameof(fps), fps,
                    "ClipRecorder.Begin fps must be > 0");
            if (_activeId != null)
            {
                // A second clip while one runs cannot interleave into the same
                // directory; abandon the first so its frames stay addressable
                // and the second starts clean.
                Log.Warning("[7dtd-playtest] clip " + _activeId + " still active; abandoning it before " + id);
                Abandon();
            }
            // A reused id must not inherit the previous take's frames: the
            // completion line names this directory, so anything left in it
            // would be muxed and counted as part of this recording.
            Helpers.ResetClipDir(id);
            _activeId = id;
            _superSize = superSize;
            _interval = 1f / fps;
            _frames = 0;
            _startedAt = Time.unscaledTime;
            _nextFrameAt = _startedAt;
            Log.Out("[7dtd-playtest] clip recording " + id + " superSize=" + superSize + " fps=" + fps);
        }

        /// <summary>Stop recording <paramref name="id"/> and emit its completion line.</summary>
        public static void End(string id)
        {
            if (string.IsNullOrEmpty(id))
                throw new ArgumentException("ClipRecorder.End needs a clip id");
            if (_activeId != id)
            {
                Log.Warning("[7dtd-playtest] clip " + id + " is not recording; nothing to end");
                return;
            }
            Finish();
        }

        /// <summary>
        /// Advance the recorder. Called every game update from the same
        /// gmUpdate hook the scenario runner uses; <paramref name="runnerFinished"/>
        /// aborts a clip a failed case left active.
        /// </summary>
        public static void Tick(bool runnerFinished)
        {
            if (_activeId == null) return;
            if (runnerFinished)
            {
                Log.Warning("[7dtd-playtest] clip " + _activeId + " left active at suite end; abandoning it");
                Abandon();
                return;
            }
            float now = Time.unscaledTime;
            if (now - _startedAt > MaxClipSeconds)
            {
                Log.Warning("[7dtd-playtest] clip " + _activeId + " exceeded "
                    + MaxClipSeconds + "s without an End; abandoning it");
                Abandon();
                return;
            }
            if (now < _nextFrameAt) return;
            Helpers.CaptureClipFrame(_activeId, _frames, _superSize);
            _frames++;
            _nextFrameAt = now + _interval;
        }

        static void Finish()
        {
            Log.Out("[7dtd-playtest] clip complete " + _activeId + " frames=" + _frames
                + " -> playtest-shots/clips/" + _activeId);
            _activeId = null;
        }

        static void Abandon()
        {
            Log.Out("[7dtd-playtest] clip abandoned " + _activeId + " frames=" + _frames
                + " -> playtest-shots/clips/" + _activeId);
            _activeId = null;
        }
    }
}
