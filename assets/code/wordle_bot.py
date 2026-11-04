#!/usr/bin/env python3
"""Screen-reading bot for a six-guess Wordle-style arcade game.

Uses the existing wordle-cli.js for recommendations, OpenCV/numpy for
reading tile colors and the cyan solved counter, and PyAutoGUI to type.
No OCR or browser DOM access required.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

CONFIG_NAME = "wordle_cv_config.json"
# Coordinates measured from the provided 1920x1080 recording.  These
# ONLY apply if the game window is still in precisely the same position.
VIDEO_BOARD = [320, 388, 309, 372]  # live screenshot
VIDEO_COUNTER = [608, 258, 92, 62]  # full cyan digits area


def rect_from_points(a, b):
    x1, y1 = a
    x2, y2 = b
    if x2 <= x1 or y2 <= y1:
        raise ValueError("Second point must be below/right of the first")
    return [int(x1), int(y1), int(x2 - x1), int(y2 - y1)]


def calibrate(config_path):
    import pyautogui as pg
    print("Keep the game visible and at the size you will use for botting.")
    def get_point(label):
        input(f"Move mouse to {label} and press ENTER here: ")
        p = pg.position()
        print("  ->", p)
        return p.x, p.y
    a = get_point("TOP-LEFT of the FIRST tile (row 1, column 1)")
    b = get_point("BOTTOM-RIGHT of the LAST tile (row 6, column 5)")
    c = get_point("TOP-LEFT of the cyan SOLVED COUNTER DIGITS only")
    d = get_point("BOTTOM-RIGHT of those digits (exclude label and border)")
    config = {"board": rect_from_points(a, b), "counter": rect_from_points(c, d)}
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print("Saved to", config_path, config)


def crop(frame, rect):
    x, y, w, h = rect
    if x < 0 or y < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
        raise RuntimeError(f"Rectangle {rect} falls outside screen {frame.shape[1]}x{frame.shape[0]}")
    return frame[y:y + h, x:x + w]


def tile_state(frame, board, row, col):
    """Sample the upper-left interior, avoiding outlines and letter glyphs.

    Colors measured from the game in the recording (BGR):
    gray=(63,35,36), yellow=(23,193,243), green=(128,220,73),
    empty=(27,13,13). Thresholds allow mild compression/scaling.
    """
    x, y, w, h = board
    cell_w, cell_h = w / 5, h / 6
    x0 = round(x + col * cell_w + cell_w * .20)
    y0 = round(y + row * cell_h + cell_h * .20)
    n = max(3, round(min(cell_w, cell_h) * .10))
    patch = frame[y0:y0 + n, x0:x0 + n, :3]
    if patch.size == 0:
        return '?'
    b, g, r = np.median(patch.reshape(-1, 3), axis=0)
    if g > 105 and g > r + 55 and g > b + 30:
        return 'G'
    if r > 140 and g > 95 and r > g + 19:
        return 'Y'
    if 42 <= b <= 130 and 23 <= g <= 105 and r < 110 and b > g + 8:
        return 'B'
    if max(b, g, r) < 56:
        return '.'  # empty tile or typed but not submitted
    return '?'  # intermediate animation / unrecognized


def board_states(frame, board):
    return [''.join(tile_state(frame, board, r, c) for c in range(5)) for r in range(6)]


def counter_mask(frame, rect):
    """Binary cyan digit mask, excluding the counter border and label."""
    area = crop(frame, rect)[:, :, :3]
    b, g, r = [area[:, :, i].astype(np.int16) for i in range(3)]
    return (b > 105) & (g > 65) & (b > r + 55) & (b > g + 9)


def counter_changed(before, after):
    """Compare the cyan counter glyphs, not the panel background."""
    if before.shape != after.shape:
        return False
    n0, n1 = np.count_nonzero(before), np.count_nonzero(after)
    # As the counter grows from 9 to 10, 19 to 20, etc., multiple cyan
    # digits can legitimately fill far more than 23% of this small crop.
    # Reject only missing/near-solid masks, not normal multi-digit values.
    if min(n0, n1) < 14 or max(n0, n1) > before.size * .80:
        return False
    changed = int(np.count_nonzero(before ^ after))
    union = int(np.count_nonzero(before | after))
    # The ones-place digit may change only a small fraction of a two-digit mask.
    return changed >= max(12, round(union * .12))


def wait_counter_increment(screen, counter, baseline, timeout=8):
    """Require a stable change of the cyan numeric counter after five greens."""
    end = time.monotonic() + timeout
    consecutive = 0
    while time.monotonic() < end:
        mask = counter_mask(screen.frame(), counter)
        consecutive = consecutive + 1 if counter_changed(baseline, mask) else 0
        if consecutive >= 6:
            return True
        time.sleep(.075)
    return False


class LiveScreen:
    def __init__(self):
        try:
            import mss
        except ImportError as ex:
            raise SystemExit("Install mss: python3 -m pip install mss") from ex
        self.sct = mss.MSS()
        self.monitor = self.sct.monitors[0]
    def frame(self):
        shot = np.asarray(self.sct.grab(self.monitor))
        return np.ascontiguousarray(shot[:, :, :3])  # BGR; assumes monitor 0 begins at 0,0
    def close(self):
        self.sct.close()


def wait_empty(screen, board, timeout=10):
    deadline = time.monotonic() + timeout
    stable = 0
    while time.monotonic() < deadline:
        states = board_states(screen.frame(), board)
        if states == ['.....'] * 6:
            stable += 1
            if stable >= 4:
                return True
        else:
            stable = 0
        time.sleep(.08)
    return False


def wait_feedback(screen, board, row, timeout=8):
    end = time.monotonic() + timeout
    previous = None
    stable = 0
    while time.monotonic() < end:
        state = board_states(screen.frame(), board)[row]
        if len(state) == 5 and all(c in 'BGY' for c in state):
            stable = stable + 1 if state == previous else 1
            previous = state
            if stable >= 4:
                return ('solved' if state == 'GGGGG' else 'feedback'), state
        else:
            previous, stable = None, 0
        time.sleep(.075)
    return 'timeout', None


def solve_cli(cli_path, history, timeout):
    cmd = ['node', str(cli_path), '--json'] + [f'{w}:{pattern}' for w, pattern in history]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=timeout, cwd=cli_path.parent)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Wordle CLI exceeded {timeout}s") from exc
    if p.returncode != 0:
        raise RuntimeError(f"Wordle CLI failed: {p.stderr.strip() or p.stdout.strip()}")
    try:
        result = json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid CLI JSON: {p.stdout[:400]!r}") from exc
    return result


def best_guess(result, history):
    used = {word for word, _ in history}
    likely = [w.upper() for w in result.get('likelyAnswers', []) if w.upper() not in used]
    if len(likely) == 1:
        return likely[0], None
    if len(likely) == 2:
        # Two probable answers: try the first, and then the other without
        # running the solver again if the first is incorrect.
        return likely[0], likely[1]
    recommended = result.get('recommendation')
    if not recommended:
        raise RuntimeError(f"No suggested guess: {result}")
    return recommended.upper(), None


def play(args, config):
    try:
        import pyautogui as pg
    except Exception as ex:
        raise SystemExit(f"PyAutoGUI unavailable: {ex}\nRun on your X11 desktop") from ex
    pg.FAILSAFE = True  # Move cursor to top-left to abort.
    pg.PAUSE = 0.01
    board, counter = config['board'], config['counter']
    cli_path = Path(args.cli).expanduser().resolve()
    if not cli_path.exists():
        raise RuntimeError(f"No CLI at {cli_path}; use --cli /path/to/wordle-cli.js")
    screen = LiveScreen()
    try:
        print('WordleCV v5 (multi-digit counter fix) | board:', board, '| counter:', counter)
        print('Abort with Ctrl+C or move mouse to the top-left corner.')
        print('Ensure the Wordle game is started and its grid is blank.')
        if not wait_empty(screen, board, args.wait):
            raise RuntimeError('Board not blank; calibrate again or start a new round')
        initial_counter_pixels = int(np.count_nonzero(counter_mask(screen.frame(), counter)))
        print(f'Counter calibration: {initial_counter_pixels} cyan pixels', flush=True)
        if not (14 <= initial_counter_pixels <= counter[2] * counter[3] * .80):
            raise RuntimeError('Counter calibration wrong: cyan digits not found. '
                               'Use --snapshot/--calibrate before running.')
        # Click the blank border beside the grid to focus the game browser.
        pg.click(board[0] - 22, board[1] + 25)
        total = 0
        while args.rounds == 0 or total < args.rounds:
            history = []
            pending_second = None
            print(f'\n=== ROUND {total + 1} ===', flush=True)
            for guess_n in range(6):
                if guess_n == 0:
                    word = 'SALET'
                elif pending_second:
                    word, pending_second = pending_second, None
                    print('Two-answer shortcut: no extra solver call')
                else:
                    outcome = solve_cli(cli_path, history, args.cli_timeout)
                    word, pending_second = best_guess(outcome, history)
                    print(f"  possible: {outcome.get('likelyCount', '?')} likely + "
                          f"{outcome.get('unlikelyCount', '?')} unlikely")
                pre = board_states(screen.frame(), board)
                if pre[guess_n] != '.....':
                    raise RuntimeError(f'Expected blank row {guess_n+1}, got {pre[guess_n]!r}; '
                                       'stopping instead of typing twice.')
                baseline = counter_mask(screen.frame(), counter)
                print(f'  {guess_n + 1}/6 -> {word}', flush=True)
                pg.write(word.lower(), interval=args.key_delay)
                pg.press('enter')
                kind, feedback = wait_feedback(screen, board, guess_n, timeout=args.wait)
                if kind == 'solved':
                    if not wait_counter_increment(screen, counter, baseline, timeout=args.wait):
                        raise RuntimeError('Five greens detected, but SOLVED counter did not increment; stopping.')
                    total += 1
                    print(f'  SOLVED! completed rounds: {total}', flush=True)
                    break
                if kind != 'feedback':
                    raise RuntimeError(f'No feedback detected for {word}; stop to inspect the screen')
                history.append((word, feedback))
                print(f'  {word}:{feedback}', flush=True)
            else:
                raise RuntimeError('Six guesses used without solving; stopping')
            if args.rounds and total >= args.rounds:
                break
            if not wait_empty(screen, board, args.wait):
                raise RuntimeError('Next board did not clear. Check game state.')
            time.sleep(args.pause)
    finally:
        screen.close()


def debug_snapshot(config, destination):
    screen = LiveScreen()
    try:
        frame = screen.frame()
    finally:
        screen.close()
    board = config["board"]
    counter = config["counter"]
    cv2.rectangle(frame, (board[0], board[1]), (board[0] + board[2], board[1] + board[3]), (0, 255, 255), 2)
    cv2.rectangle(frame, (counter[0], counter[1]), (counter[0] + counter[2], counter[1] + counter[3]), (0, 255, 255), 2)
    for r in range(6):
        for c in range(5):
            cw, ch = board[2] / 5, board[3] / 6
            x = round(board[0] + c * cw + cw * .20)
            y = round(board[1] + r * ch + ch * .20)
            cv2.circle(frame, (x, y), 4, (0, 0, 255), -1)
    cv2.imwrite(str(destination), frame)
    print("Snapshot:", destination, "| row states:", board_states(frame, board))


def inspect_video(path, config):
    """Offline sanity-check for sampled video footage; does not type or call solver."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError('Could not open video ' + str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f'Video: {frames} frames, {fps:.2f} FPS, {frames/fps:.1f}s')
    last = None
    for s in range(0, int(frames/fps), 3):
        cap.set(cv2.CAP_PROP_POS_MSEC, s * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        states = board_states(frame, config['board'])
        completed = [f'{i+1}:{row}' for i, row in enumerate(states)
                     if all(c in 'BGY' for c in row)]
        signature = tuple(completed)
        if signature != last:
            print(f't={s:3d}s', ' '.join(completed) or '[blank/transition]')
            last = signature
    cap.release()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cli', default='./wordle-cli.js', help='path to existing JS solver')
    ap.add_argument('--config', default=CONFIG_NAME)
    ap.add_argument('--calibrate', action='store_true', help='record board and counter positions')
    ap.add_argument('--video-layout', action='store_true', help='use 1920x1080 VIDEO positions')
    ap.add_argument('--test-video', help='offline color detection test; NEVER types')
    ap.add_argument('--snapshot', nargs='?', const='wordle_cv_debug.png',
                    help='save marked-up screenshot, without typing')
    ap.add_argument('--rounds', type=int, default=0, help='0=until stopped')
    ap.add_argument('--wait', type=float, default=8.0, help='seconds to wait for UI')
    ap.add_argument('--pause', type=float, default=.35, help='seconds after board clears')
    ap.add_argument('--key-delay', type=float, default=.025)
    ap.add_argument('--cli-timeout', type=float, default=25.0)
    args = ap.parse_args()
    path = Path(args.config)
    if args.calibrate:
        calibrate(path)
        return
    if args.video_layout:
        config = {'board': VIDEO_BOARD, 'counter': VIDEO_COUNTER}
    elif path.exists():
        config = json.loads(path.read_text())
    elif args.test_video:
        config = {'board': VIDEO_BOARD, 'counter': VIDEO_COUNTER}
    else:
        raise RuntimeError('Run with --calibrate first (or --video-layout if identical to video)')
    if args.test_video:
        inspect_video(Path(args.test_video), config)
    elif args.snapshot:
        debug_snapshot(config, Path(args.snapshot))
    else:
        play(args, config)


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, Exception) as e:
        if isinstance(e, KeyboardInterrupt):
            print('\nStopped.')
        else:
            print(f'ERROR: {e}', file=sys.stderr)
            sys.exit(1)
