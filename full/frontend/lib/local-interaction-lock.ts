let holders = 0;
let shells: { element: HTMLElement; inert: boolean }[] = [];
/** Shared nesting prevents an import and a simultaneous native close from unlocking each other. */
export function lockLocalInteraction() {
  if (holders++ === 0) {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    shells = [...document.querySelectorAll<HTMLElement>("body")].map(element => ({ element, inert: element.inert }));
    shells.forEach(({ element }) => { element.inert = true; });
  }
  let released = false;
  return () => {
    if (released) return; released = true;
    if (--holders === 0) { shells.forEach(({ element, inert }) => { element.inert = inert; }); shells = []; }
  };
}