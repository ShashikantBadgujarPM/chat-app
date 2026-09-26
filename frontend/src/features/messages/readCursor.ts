// When to move the read cursor (M07): only while the conversation is really being read
// (tab visible, window focused, scrolled to the latest message), debounced so a burst of
// scroll or message updates becomes one PUT. The server keeps the cursor monotonic
// (GREATEST), so an occasional stale send is harmless.

export type ReadActivity = {
  visible: boolean
  focused: boolean
  atBottom: boolean
}

export function isReading(activity: ReadActivity): boolean {
  return activity.visible && activity.focused && activity.atBottom
}

type Timer = ReturnType<typeof setTimeout>

export class CursorSender {
  private timer: Timer | null = null
  private pendingSeq = 0
  private sentSeq: number
  private readonly send: (seq: number) => void
  private readonly delayMs: number

  constructor(send: (seq: number) => void, delayMs = 500, initialSeq = 0) {
    this.send = send
    this.delayMs = delayMs
    this.sentSeq = initialSeq
  }

  /** Called whenever the latest visible seq or the reading state changes. */
  update(latestSeq: number, activity: ReadActivity): void {
    if (!isReading(activity) || latestSeq <= this.sentSeq) return
    this.pendingSeq = Math.max(this.pendingSeq, latestSeq)
    if (this.timer) clearTimeout(this.timer)
    this.timer = setTimeout(() => {
      this.timer = null
      if (this.pendingSeq > this.sentSeq) {
        this.sentSeq = this.pendingSeq
        this.send(this.sentSeq)
      }
    }, this.delayMs)
  }

  /** The server moved the cursor (e.g. another tab read further). */
  acknowledge(seq: number): void {
    this.sentSeq = Math.max(this.sentSeq, seq)
  }

  dispose(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
  }
}
