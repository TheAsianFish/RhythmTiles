import type { SkinId } from "./skins";

/** Sets `data-bb-skin` on the document root so skins.css applies. */
export function applyDocumentSkin(skinId: SkinId): void {
  document.documentElement.dataset.bbSkin = skinId;
}
