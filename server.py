"""
Con Alma Guaraní — Bot WhatsApp v2
Lee el catálogo desde OneDrive para mantenerse sincronizado con Excel.
"""

import os, json, re, csv, datetime, httpx, base64
from pathlib import Path
from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID   = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_FROM = os.environ["TWILIO_WHATSAPP_FROM"]
OWNER_WHATSAPP       = os.environ["OWNER_WHATSAPP"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]

# URL pública del catalog_productos.json en OneDrive
# En OneDrive: click derecho → Compartir → Cualquiera con el vínculo → Copiar
# Reemplazá "?..." por "?download=1" al final
ONEDRIVE_CATALOG_URL = os.environ.get("ONEDRIVE_CATALOG_URL", "")

VENDEDORAS = {
    os.environ.get("VENDEDORA_1", ""): os.environ.get("VENDEDORA_1_NOMBRE", "Vendedora 1"),
    os.environ.get("VENDEDORA_2", ""): os.environ.get("VENDEDORA_2_NOMBRE", "Vendedora 2"),
}

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Catálogo con caché (se recarga cada 5 minutos) ─────────────────────────────
_catalog_cache: dict = {}
_catalog_loaded_at: datetime.datetime | None = None
CACHE_TTL_MINUTES = 5

def load_catalog() -> dict:
    global _catalog_cache, _catalog_loaded_at
    now = datetime.datetime.now()
    if (_catalog_loaded_at and
            (now - _catalog_loaded_at).seconds < CACHE_TTL_MINUTES * 60 and
            _catalog_cache):
        return _catalog_cache

    # Intentar cargar desde OneDrive
    if ONEDRIVE_CATALOG_URL:
        try:
            r = httpx.get(ONEDRIVE_CATALOG_URL, follow_redirects=True, timeout=10)
            _catalog_cache = r.json()
            _catalog_loaded_at = now
            print(f"✅ Catálogo cargado desde OneDrive: {len(_catalog_cache.get('productos', []))} productos")
            return _catalog_cache
        except Exception as e:
            print(f"⚠️ No pude cargar desde OneDrive: {e} — usando catálogo local")

    # Fallback: catálogo local
    local = Path(__file__).parent / "catalog_productos.json"
    if local.exists():
        _catalog_cache = json.loads(local.read_text(encoding="utf-8"))
        _catalog_loaded_at = now
        return _catalog_cache

    return {"productos": [], "prefijos": {}}

def build_catalog_text(catalog: dict) -> str:
    productos = [p for p in catalog.get("productos", []) if p.get("activo", True)]
    lines = []
    for p in productos:
        pv = f"${p['precio_venta']:,.0f}" if p.get("precio_venta") else "sin precio"
        lines.append(f"{p['id']:<12} {p['nombre']:<45} venta: {pv}")
    return "\n".join(lines)

# ── Historial de ventas del día ────────────────────────────────────────────────
ventas_hoy: list[dict] = []
CSV_PATH = Path(__file__).parent / "ventas.csv"

def guardar_venta(venta: dict):
    ventas_hoy.append(venta)
    file_exists = CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "fecha", "hora", "vendedora", "articulo", "nombre_producto",
            "cantidad", "precio_unitario", "medio_pago", "total", "notas"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow(venta)

# ── Transcribir audio ──────────────────────────────────────────────────────────
def transcribir_audio(media_url: str, content_type: str) -> str:
    auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    r = httpx.get(media_url, auth=auth, follow_redirects=True, timeout=30)
    audio_b64 = base64.b64encode(r.content).decode()

    resp = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "Transcribí exactamente este audio. Solo devolvé la transcripción, sin comentarios."},
                {"type": "document", "source": {"type": "base64", "media_type": content_type, "data": audio_b64}}
            ]
        }]
    )
    return resp.content[0].text.strip()

# ── Interpretar venta ──────────────────────────────────────────────────────────
def interpretar_venta(texto: str, catalog: dict) -> dict:
    catalog_text = build_catalog_text(catalog)

    system = f"""Sos el asistente de ventas de "Con Alma Guaraní", tienda de artesanías argentinas.

CATÁLOGO ACTUAL ({len(catalog.get('productos', []))} productos):
{catalog_text}

TAREA: Interpretar el mensaje y extraer ventas. SOLO devolvés JSON válido sin texto extra.

REGLAS:
- Identificá productos aunque el nombre esté incompleto
- Si el precio no se menciona, usá el precio_venta del catálogo  
- Medio de pago: TRANSFERENCIA / EFECTIVO / TARJETA DE CREDITO / DEBITO / QR
- Si hay varias ventas, devolvé todas

FORMATO:
{{
  "ventas": [
    {{
      "articulo": "AMA-0003",
      "nombre_producto": "VASITOS",
      "cantidad": 2,
      "precio_unitario": 26000,
      "costo_unitario": 20000,
      "medio_pago": "TRANSFERENCIA",
      "notas": ""
    }}
  ],
  "dudas": ""
}}"""

    resp = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": texto}]
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

# ── Formatear mensajes ─────────────────────────────────────────────────────────
def fmt_confirmacion(resultado: dict, vendedora: str) -> str:
    ventas = resultado.get("ventas", [])
    if not ventas:
        return "⚠️ No pude identificar la venta. ¿Podés repetirlo?"
    lines = [f"✅ *Venta registrada — {vendedora}*\n"]
    total = 0
    for v in ventas:
        t = v.get("precio_unitario", 0) * v.get("cantidad", 1)
        total += t
        lines.append(f"• {v['nombre_producto']} x{v['cantidad']}\n  ${v['precio_unitario']:,} c/u → *${t:,}*\n  💳 {v['medio_pago']}")
        if v.get("notas"):
            lines.append(f"  📝 {v['notas']}")
    if len(ventas) > 1:
        lines.append(f"\n💰 *TOTAL: ${total:,}*")
    if resultado.get("dudas"):
        lines.append(f"\n⚠️ _{resultado['dudas']}_")
    return "\n".join(lines)

def fmt_para_dueno(resultado: dict, vendedora: str, hora: str) -> str:
    ventas = resultado.get("ventas", [])
    total  = sum(v.get("precio_unitario", 0) * v.get("cantidad", 1) for v in ventas)
    lines  = [f"🛒 *{hora} — {vendedora}*\n"]
    for v in ventas:
        t = v.get("precio_unitario", 0) * v.get("cantidad", 1)
        lines.append(f"• {v['nombre_producto']} x{v['cantidad']} = *${t:,}* ({v['medio_pago']})")
    lines.append(f"\n💰 *${total:,}*")
    return "\n".join(lines)

def fmt_resumen_diario() -> str:
    if not ventas_hoy:
        return f"📊 *Resumen del día — {datetime.date.today().strftime('%d/%m/%Y')}*\n\nSin ventas registradas todavía."
    total       = sum(v["total"] for v in ventas_hoy)
    por_vend    = {}
    por_medio   = {}
    for v in ventas_hoy:
        por_vend[v["vendedora"]]  = por_vend.get(v["vendedora"], 0)  + v["total"]
        por_medio[v["medio_pago"]] = por_medio.get(v["medio_pago"], 0) + v["total"]
    lines = [
        f"📊 *Resumen — {datetime.date.today().strftime('%d/%m/%Y')}*\n",
        f"🧾 Ventas: {len(ventas_hoy)} | 💰 Total: *${total:,}*\n",
        "*Por vendedora:*",
        *[f"  • {n}: ${m:,}" for n, m in por_vend.items()],
        "\n*Por medio de pago:*",
        *[f"  • {mp}: ${m:,}" for mp, m in sorted(por_medio.items(), key=lambda x: -x[1])],
    ]
    return "\n".join(lines)

def enviar_whatsapp(to: str, msg: str):
    twilio_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to, body=msg)

# ── Webhook ────────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    from_number  = request.form.get("From", "")
    body         = request.form.get("Body", "").strip()
    num_media    = int(request.form.get("NumMedia", 0))
    media_url    = request.form.get("MediaUrl0", "")
    media_type   = request.form.get("MediaContentType0", "")

    numero_limpio = from_number.replace("whatsapp:", "").replace("+", "")
    is_owner = OWNER_WHATSAPP.replace("whatsapp:", "").replace("+", "") in numero_limpio

    # Comandos del dueño
    if is_owner:
        if "resumen" in body.lower():
            enviar_whatsapp(OWNER_WHATSAPP, fmt_resumen_diario())
            return Response("<Response/>", mimetype="text/xml")
        if "reload" in body.lower():
            global _catalog_loaded_at
            _catalog_loaded_at = None
            catalog = load_catalog()
            n = len(catalog.get("productos", []))
            enviar_whatsapp(OWNER_WHATSAPP, f"🔄 Catálogo recargado: {n} productos.")
            return Response("<Response/>", mimetype="text/xml")

    # Identificar vendedora
    vendedora = None
    for num, nombre in VENDEDORAS.items():
        if num and numero_limpio.endswith(num.replace("+", "")):
            vendedora = nombre
            break
    if not vendedora and not is_owner:
        resp = MessagingResponse()
        resp.message("❌ Número no autorizado.")
        return Response(str(resp), mimetype="text/xml")
    if not vendedora:
        vendedora = "Dueño"

    # Transcribir audio
    texto = body
    if num_media > 0 and "audio" in media_type:
        try:
            texto = transcribir_audio(media_url, media_type)
            enviar_whatsapp(from_number, f'🎤 _"{texto}"_\n\n_Procesando..._')
        except Exception as e:
            enviar_whatsapp(from_number, f"⚠️ No pude transcribir el audio: {e}")
            return Response("<Response/>", mimetype="text/xml")

    if not texto:
        enviar_whatsapp(from_number,
            "📝 Mandame una nota de voz o escribí la venta, por ejemplo:\n_\"2 vasitos y una taza colonial, transferencia\"_")
        return Response("<Response/>", mimetype="text/xml")

    # Cargar catálogo (con caché)
    catalog = load_catalog()

    # Interpretar
    try:
        resultado = interpretar_venta(texto, catalog)
    except Exception as e:
        enviar_whatsapp(from_number, f"⚠️ Error: {e}")
        return Response("<Response/>", mimetype="text/xml")

    # Guardar
    ahora = datetime.datetime.now()
    for v in resultado.get("ventas", []):
        guardar_venta({
            "fecha":           ahora.strftime("%d/%m/%Y"),
            "hora":            ahora.strftime("%H:%M"),
            "vendedora":       vendedora,
            "articulo":        v.get("articulo", ""),
            "nombre_producto": v.get("nombre_producto", ""),
            "cantidad":        v.get("cantidad", 1),
            "precio_unitario": v.get("precio_unitario", 0),
            "medio_pago":      v.get("medio_pago", ""),
            "total":           v.get("precio_unitario", 0) * v.get("cantidad", 1),
            "notas":           v.get("notas", ""),
        })

    enviar_whatsapp(from_number, fmt_confirmacion(resultado, vendedora))
    if not is_owner:
        enviar_whatsapp(OWNER_WHATSAPP, fmt_para_dueno(resultado, vendedora, ahora.strftime("%H:%M")))

    return Response("<Response/>", mimetype="text/xml")

# ── Descargar CSV ──────────────────────────────────────────────────────────────
@app.route("/descargar-csv")
def descargar_csv():
    if request.args.get("token") != os.environ.get("DOWNLOAD_TOKEN", "alma2026"):
        return "No autorizado", 403
    if not CSV_PATH.exists():
        return "Sin ventas todavía", 404
    return Response(
        CSV_PATH.read_text(encoding="utf-8"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ventas_{datetime.date.today()}.csv"}
    )

@app.route("/catalog-info")
def catalog_info():
    if request.args.get("token") != os.environ.get("DOWNLOAD_TOKEN", "alma2026"):
        return "No autorizado", 403
    cat = load_catalog()
    return {"productos": len(cat.get("productos", [])),
            "ultima_actualizacion": cat.get("ultima_actualizacion", "desconocida"),
            "fuente": "OneDrive" if ONEDRIVE_CATALOG_URL else "local"}

@app.route("/")
def home():
    return "✅ Con Alma Guaraní Bot v2 — activo"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
