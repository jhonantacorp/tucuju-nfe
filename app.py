"""
Download de XML de NF-e por chave de acesso.
Versao parametrizada por variaveis de ambiente (serve para qualquer cliente).
Deploy: Render.com
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests as req_lib
import xml.etree.ElementTree as ET
import tempfile, os, re, io, logging, gzip, base64
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuracao via variaveis de ambiente (Render) ──────────────────────────
CERT_B64      = os.environ.get("CERT_B64", "")
CERT_PASSWORD = os.environ.get("CERT_PASSWORD", "")
AMBIENTE      = os.environ.get("AMBIENTE", "1")        # 1 = Producao
CUF           = os.environ.get("CUF", "16")            # 16 = Amapa
EMPRESA       = os.environ.get("EMPRESA", "Farmacia")  # nome exibido na tela
SUBTITULO     = os.environ.get("SUBTITULO", "Consulta NF-e")
# CNPJS: um ou varios, separados por virgula (so numeros)
CNPJS = [c.strip() for c in os.environ.get("CNPJS", "").split(",") if c.strip()]
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

SEFAZ_URL   = "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx"
NS_DIST     = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe"
NS_NFE      = "http://www.portalfiscal.inf.br/nfe"
SOAP_ACTION = '"' + NS_DIST + '/nfeDistDFeInteresse"'

_HTML = open(os.path.join(os.path.dirname(__file__), "templates", "index.html"),
             encoding="utf-8").read()
HTML = _HTML.replace("{{EMPRESA}}", EMPRESA).replace("{{SUBTITULO}}", SUBTITULO)


def extrair_pem():
    from cryptography.hazmat.primitives.serialization import (
        pkcs12, Encoding, PrivateFormat, NoEncryption)
    pfx_data = base64.b64decode(CERT_B64)
    pk, cert, _ = pkcs12.load_key_and_certificates(pfx_data, CERT_PASSWORD.encode())
    fc = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    fc.write(cert.public_bytes(Encoding.PEM)); fc.close()
    fk = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    fk.write(pk.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL,
                              NoEncryption())); fk.close()
    return fc.name, fk.name


def montar_soap(cnpj, chave):
    inner = (
        '<distDFeInt versao="1.01" xmlns="' + NS_NFE + '">'
        '<tpAmb>' + AMBIENTE + '</tpAmb>'
        '<cUFAutor>' + CUF + '</cUFAutor>'
        '<CNPJ>' + cnpj + '</CNPJ>'
        '<consChNFe><chNFe>' + chave + '</chNFe></consChNFe>'
        '</distDFeInt>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        '<nfeDistDFeInteresse xmlns="' + NS_DIST + '">'
        '<nfeDadosMsg>' + inner + '</nfeDadosMsg>'
        '</nfeDistDFeInteresse>'
        '</soap:Body>'
        '</soap:Envelope>'
    )


def tentar_cnpj(cnpj, chave, cert_tmp, key_tmp):
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
    resp = req_lib.post(SEFAZ_URL, data=montar_soap(cnpj, chave).encode("utf-8"),
                        headers=headers, cert=(cert_tmp, key_tmp),
                        verify=False, timeout=20)
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None

    fault = (root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
             or root.find(".//Fault"))
    if fault is not None:
        logging.warning("SOAP Fault CNPJ %s", cnpj)
        return None

    cstat_el = root.find(".//{" + NS_NFE + "}cStat")
    status = cstat_el.text if cstat_el is not None else "?"
    logging.info("CNPJ %s -> cStat=%s", cnpj, status)

    if status not in {"137", "138"}:
        return None

    for doc in root.findall(".//{" + NS_NFE + "}docZip"):
        try:
            return gzip.decompress(base64.b64decode(doc.text)).decode("utf-8")
        except Exception:
            continue
    return None


@app.route("/")
def index():
    return HTML


@app.route("/consultar", methods=["POST"])
def consultar():
    data = request.get_json(force=True)
    chave = re.sub(r"\D", "", data.get("chave", ""))

    if len(chave) != 44:
        return jsonify({"erro": "Chave de acesso invalida. Deve ter 44 digitos."}), 400
    if not CERT_B64:
        return jsonify({"erro": "Certificado nao configurado (variavel CERT_B64)."}), 500
    if not CNPJS:
        return jsonify({"erro": "CNPJ nao configurado (variavel CNPJS)."}), 500

    cert_tmp, key_tmp = extrair_pem()
    try:
        for cnpj in CNPJS:
            xml_nfe = tentar_cnpj(cnpj, chave, cert_tmp, key_tmp)
            if xml_nfe:
                buf = io.BytesIO(xml_nfe.encode("utf-8")); buf.seek(0)
                return send_file(buf, mimetype="application/xml", as_attachment=True,
                                 download_name="NF-e_" + chave + ".xml")
        return jsonify({"erro": "NF-e nao encontrada. Verifique a chave de acesso."}), 404
    except Exception as e:
        logging.exception("Erro")
        return jsonify({"erro": str(e)}), 500
    finally:
        os.unlink(cert_tmp); os.unlink(key_tmp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
