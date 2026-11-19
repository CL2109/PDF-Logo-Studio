import base64
import io
import json
import os
import re
import zipfile
from pathlib import Path

import fitz  # PyMuPDF
import streamlit as st
from PIL import Image

st.set_page_config(
    page_title="PDF Logo Studio",
    page_icon="📄",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Utilitaires : encodage/décodage logos & compatibilité JSON
# ---------------------------------------------------------------------------

def image_bytes_to_data_url(img_bytes: bytes) -> str:
    """Convertit une image (PNG/JPG/WebP) en Data URL PNG."""
    with Image.open(io.BytesIO(img_bytes)) as img:
        img = img.convert("RGBA")
        out = io.BytesIO()
        img.save(out, format="PNG")
        b64 = base64.b64encode(out.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{b64}"


def data_url_to_bytes(data_url: str) -> bytes:
    """Extrait les octets bruts d'une Data URL ou d'une chaîne Base64."""
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    return base64.b64decode(data_url)


def parse_logos_json(raw_data) -> dict[str, str]:
    """
    Accepte plusieurs formats JSON (compatible avec le convertisseur HTML) :
    - Liste d'objets : [{"name": "Client A", "dataUrl": "data:image/..."}]
    - Objet contenant "logos" : {"logos": [...]}
    - Dictionnaire simple : {"Client A": "data:image/..."}
    """
    logos: dict[str, str] = {}

    if isinstance(raw_data, dict) and "logos" in raw_data and isinstance(raw_data["logos"], list):
        items = raw_data["logos"]
    elif isinstance(raw_data, list):
        items = raw_data
    elif isinstance(raw_data, dict):
        for k, v in raw_data.items():
            if isinstance(v, str):
                logos[str(k)] = v
        return logos
    else:
        return logos

    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("nom") or item.get("client")
        data = item.get("dataUrl") or item.get("data") or item.get("base64") or item.get("src")
        if name and data:
            logos[str(name)] = str(data)

    return logos


def export_logos_json(logos: dict[str, str]) -> str:
    """Exporte la base au format JSON standard."""
    payload = [{"name": name, "dataUrl": data_url} for name, data_url in sorted(logos.items())]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def parse_pages(spec: str, total_pages: int) -> list[int]:
    """Convertit une chaîne comme '1, 3-4' en indices de pages (base 0)."""
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                for p in range(int(start), int(end) + 1):
                    if 1 <= p <= total_pages:
                        pages.add(p - 1)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p - 1)
            except ValueError:
                continue
    return sorted(pages)


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9À-ÿ._-]+", "_", name).strip("_")


# ---------------------------------------------------------------------------
# Traitement PDF (aperçu & génération)
# ---------------------------------------------------------------------------

def apply_logo_to_pdf(
    pdf_bytes: bytes,
    logo_bytes: bytes,
    x_pct: float,
    y_pct: float,
    width_pct: float,
    target_pages: list[int],
    white_bg: bool = False,
    bg_padding_pt: float = 4.0,
) -> bytes:
    """Insère le logo sur les pages sélectionnées et retourne le PDF modifié."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    with Image.open(io.BytesIO(logo_bytes)) as img:
        img_w, img_h = img.size
        aspect_ratio = img_h / img_w if img_w else 1.0

    for page_idx in target_pages:
        if page_idx < 0 or page_idx >= len(doc):
            continue
        page = doc[page_idx]
        rect_page = page.rect

        logo_w = rect_page.width * (width_pct / 100.0)
        logo_h = logo_w * aspect_ratio

        x0 = rect_page.width * (x_pct / 100.0)
        y0 = rect_page.height * (y_pct / 100.0)
        logo_rect = fitz.Rect(x0, y0, x0 + logo_w, y0 + logo_h)

        if white_bg:
            bg_rect = fitz.Rect(
                logo_rect.x0 - bg_padding_pt,
                logo_rect.y0 - bg_padding_pt,
                logo_rect.x1 + bg_padding_pt,
                logo_rect.y1 + bg_padding_pt,
            )
            page.draw_rect(bg_rect, color=None, fill=(1, 1, 1), overlay=True)

        page.insert_image(logo_rect, stream=logo_bytes, keep_proportion=True, overlay=True)

    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    return output.getvalue()


def render_page_image(pdf_bytes: bytes, page_index: int = 0, zoom: float = 1.6) -> bytes:
    """Rend une page PDF en image PNG pour l'aperçu Streamlit."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_index]
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img_bytes = pix.tobytes("png")
    doc.close()
    return img_bytes


# ---------------------------------------------------------------------------
# Initialisation de l'état (Session State)
# ---------------------------------------------------------------------------

if "logos" not in st.session_state:
    st.session_state.logos = {}
    default_json = Path("logos.json")
    if default_json.exists():
        try:
            st.session_state.logos = parse_logos_json(json.loads(default_json.read_text(encoding="utf-8")))
        except Exception:
            pass

if "pdf_bytes" not in st.session_state:
    st.session_state.pdf_bytes = None
    st.session_state.pdf_name = "document.pdf"
    default_pdf = Path("template.pdf")
    if default_pdf.exists():
        st.session_state.pdf_bytes = default_pdf.read_bytes()
        st.session_state.pdf_name = default_pdf.name

# Paramètres de positionnement par défaut
defaults = {
    "x_pct": 5.0,
    "y_pct": 4.0,
    "width_pct": 20.0,
    "white_bg": True,
    "bg_padding": 4.0,
}
for key, val in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = val


# ---------------------------------------------------------------------------
# Interface : Barre latérale (Base de données de logos)
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🗂️ Base de logos")

    # 1. Importer une sauvegarde JSON
    uploaded_json = st.file_uploader(
        "Importer une sauvegarde (.json)",
        type=["json"],
        help="Compatible avec le fichier généré par le Gestionnaire de Logos JSON.",
    )
    if uploaded_json is not None:
        if st.button("Charger / Fusionner le JSON", use_container_width=True):
            try:
                loaded = parse_logos_json(json.loads(uploaded_json.read().decode("utf-8")))
                st.session_state.logos.update(loaded)
                st.success(f"{len(loaded)} logo(s) chargé(s) !")
            except Exception as e:
                st.error(f"Erreur de lecture JSON : {e}")

    st.divider()

    # 2. Ajouter des logos manuellement
    with st.expander("➕ Ajouter des logos (PNG / JPG)", expanded=len(st.session_state.logos) == 0):
        new_files = st.file_uploader(
            "Glissez vos fichiers images",
            type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
        )
        if new_files and st.button("Ajouter à la base", use_container_width=True):
            for f in new_files:
                client_name = Path(f.name).stem
                st.session_state.logos[client_name] = image_bytes_to_data_url(f.read())
            st.success(f"{len(new_files)} logo(s) ajouté(s).")
            st.rerun()

    # 3. Gestion / Suppression et Sauvegarde JSON
    st.caption(f"**{len(st.session_state.logos)}** logo(s) en mémoire")

    if st.session_state.logos:
        st.download_button(
            label="💾 Télécharger la sauvegarde JSON",
            data=export_logos_json(st.session_state.logos),
            file_name="logos_clients.json",
            mime="application/json",
            use_container_width=True,
        )

        with st.expander("🗑️ Supprimer un logo"):
            to_delete = st.selectbox("Logo à retirer", sorted(st.session_state.logos.keys()))
            if st.button("Supprimer", type="secondary", use_container_width=True):
                del st.session_state.logos[to_delete]
                st.rerun()


# ---------------------------------------------------------------------------
# Interface principale
# ---------------------------------------------------------------------------

st.title("📄 PDF Logo Studio")
st.caption("Importez votre PDF modèle, sélectionnez le client et téléchargez le document mis à jour.")

col_controls, col_preview = st.columns([1, 1.4], gap="large")

with col_controls:
    # 1. Fichier PDF
    st.subheader("1. Document PDF modèle")
    uploaded_pdf = st.file_uploader("Glissez votre PDF à mettre à jour", type=["pdf"])
    if uploaded_pdf is not None:
        st.session_state.pdf_bytes = uploaded_pdf.read()
        st.session_state.pdf_name = uploaded_pdf.name

    if st.session_state.pdf_bytes is None:
        st.info("Veuillez charger un fichier PDF pour commencer.")
        st.stop()

    doc_info = fitz.open(stream=st.session_state.pdf_bytes, filetype="pdf")
    total_pages = len(doc_info)
    doc_info.close()
    st.success(f"Fichier actif : **{st.session_state.pdf_name}** ({total_pages} page(s))")

    # 2. Sélection du client
    st.subheader("2. Sélection du client")
    if not st.session_state.logos:
        st.warning("Ajoutez au moins un logo ou importez votre fichier `logos.json` dans le menu de gauche.")
        st.stop()

    client_names = sorted(st.session_state.logos.keys())
    selected_client = st.selectbox("Choisir le logo client", client_names)

    logo_bytes = data_url_to_bytes(st.session_state.logos[selected_client])
    st.image(logo_bytes, width=160, caption=f"Logo sélectionné : {selected_client}")

    # 3. Positionnement & options
    st.subheader("3. Positionnement du logo")

    c1, c2 = st.columns(2)
    with c1:
        x_pct = st.slider("Position X (% gauche)", 0.0, 95.0, float(st.session_state.x_pct), 0.5)
    with c2:
        y_pct = st.slider("Position Y (% haut)", 0.0, 95.0, float(st.session_state.y_pct), 0.5)

    width_pct = st.slider("Largeur du logo (% page)", 2.0, 80.0, float(st.session_state.width_pct), 0.5)

    st.session_state.x_pct = x_pct
    st.session_state.y_pct = y_pct
    st.session_state.width_pct = width_pct

    white_bg = st.checkbox(
        "Masquer l'ancien logo (fond blanc opaque)",
        value=st.session_state.white_bg,
    )
    bg_padding = 0.0
    if white_bg:
        bg_padding = st.slider("Marge du fond blanc (pt)", 0.0, 20.0, float(st.session_state.bg_padding), 1.0)

    st.session_state.white_bg = white_bg
    st.session_state.bg_padding = bg_padding

    # Pages cibles
    page_mode = st.radio(
        "Pages à mettre à jour",
        ["Première page uniquement", "Toutes les pages", "Pages personnalisées"],
        horizontal=True,
    )
    if page_mode == "Première page uniquement":
        target_pages = [0]
    elif page_mode == "Toutes les pages":
        target_pages = list(range(total_pages))
    else:
        spec = st.text_input("Numéros de pages (ex: 1, 3-5)", value="1")
        target_pages = parse_pages(spec, total_pages) or [0]

    # Sauvegarde / chargement rapide d'un profil de position
    with st.expander("⚙️ Exporter / Importer le réglage de position"):
        preset = {
            "x_pct": x_pct,
            "y_pct": y_pct,
            "width_pct": width_pct,
            "white_bg": white_bg,
            "bg_padding": bg_padding,
        }
        st.download_button(
            "Sauvegarder cette position (.json)",
            data=json.dumps(preset, indent=2),
            file_name="position_logo.json",
            mime="application/json",
            use_container_width=True,
        )
        preset_file = st.file_uploader("Charger un réglage (.json)", type=["json"], key="preset_up")
        if preset_file is not None and st.button("Appliquer ce réglage", use_container_width=True):
            try:
                p = json.loads(preset_file.read().decode("utf-8"))
                st.session_state.x_pct = float(p.get("x_pct", x_pct))
                st.session_state.y_pct = float(p.get("y_pct", y_pct))
                st.session_state.width_pct = float(p.get("width_pct", width_pct))
                st.session_state.white_bg = bool(p.get("white_bg", white_bg))
                st.session_state.bg_padding = float(p.get("bg_padding", bg_padding))
                st.rerun()
            except Exception as e:
                st.error(f"Fichier invalide : {e}")

# ---------------------------------------------------------------------------
# Génération du PDF & Aperçu en direct
# ---------------------------------------------------------------------------

updated_pdf_bytes = apply_logo_to_pdf(
    pdf_bytes=st.session_state.pdf_bytes,
    logo_bytes=logo_bytes,
    x_pct=x_pct,
    y_pct=y_pct,
    width_pct=width_pct,
    target_pages=target_pages,
    white_bg=white_bg,
    bg_padding_pt=bg_padding,
)

with col_preview:
    st.subheader("Aperçu en direct")

    if total_pages > 1:
        preview_page = st.number_input("Page à prévisualiser", min_value=1, max_value=total_pages, value=1) - 1
    else:
        preview_page = 0

    preview_img = render_page_image(updated_pdf_bytes, page_index=preview_page)
    st.image(preview_img, use_container_width=True, caption=f"Page {preview_page + 1} / {total_pages}")

    # Téléchargement individuel
    base_stem = Path(st.session_state.pdf_name).stem
    out_filename = f"{base_stem}_{sanitize_filename(selected_client)}.pdf"

    st.download_button(
        label=f"⬇️ Télécharger le PDF ({selected_client})",
        data=updated_pdf_bytes,
        file_name=out_filename,
        mime="application/pdf",
        type="primary",
        use_container_width=True,
    )

    # Export en lot (ZIP)
    if len(client_names) > 1:
        with st.expander("📦 Export en lot (générer plusieurs clients d'un coup)"):
            batch_clients = st.multiselect("Sélectionnez les clients", client_names, default=client_names)
            if batch_clients and st.button("Générer l'archive ZIP", use_container_width=True):
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for cname in batch_clients:
                        c_logo = data_url_to_bytes(st.session_state.logos[cname])
                        c_pdf = apply_logo_to_pdf(
                            pdf_bytes=st.session_state.pdf_bytes,
                            logo_bytes=c_logo,
                            x_pct=x_pct,
                            y_pct=y_pct,
                            width_pct=width_pct,
                            target_pages=target_pages,
                            white_bg=white_bg,
                            bg_padding_pt=bg_padding,
                        )
                        zf.writestr(f"{base_stem}_{sanitize_filename(cname)}.pdf", c_pdf)
                st.download_button(
                    label=f"⬇️ Télécharger les {len(batch_clients)} PDF (.zip)",
                    data=zip_buffer.getvalue(),
                    file_name=f"{base_stem}_tous_clients.zip",
                    mime="application/zip",
                    use_container_width=True,
                )