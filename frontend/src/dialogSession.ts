/** Own the browser state for one dialog, including native stock-detail dialogs. */
export class DialogSession {
  private readonly opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  private readonly overflow = document.body.style.overflow;
  private disposed = false;

  constructor(private readonly element: HTMLElement) {
    document.body.style.overflow = "hidden";
    if (element instanceof HTMLDialogElement) element.showModal();
    else element.focus();
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    if (this.element instanceof HTMLDialogElement) this.element.close();
    document.body.style.overflow = this.overflow;
    if (this.opener?.isConnected) this.opener.focus({ preventScroll: true });
  }
}
