"""
Download de XML de NF-e por chave de acesso.
- Consulta o XML completo na Distribuicao DF-e (Ambiente Nacional)
- Se a SEFAZ devolver apenas o resumo (resNFe), registra a Ciencia da
  Operacao (evento 210210) e consulta novamente
Configuracao 100% por variaveis de ambiente (Render).
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests as req_lib
from lxml import etree
import tempfile, os, re, io, logging, gzip, base64, hashlib, time, datetime
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuracao ─────────────────────────────────────────────────────────────
CERT_B64      = os.environ.get("CERT_B64", "")
CERT_PASSWORD = os.environ.get("CERT_PASSWORD", "")
AMBIENTE      = os.environ.get("AMBIENTE", "1")
CUF           = os.environ.get("CUF", "16")
EMPRESA       = os.environ.get("EMPRESA", "Farmacia")
SUBTITULO     = os.environ.get("SUBTITULO", "Consulta NF-e")
CNPJS = [c.strip() for c in os.environ.get("CNPJS", "").split(",") if c.strip()]
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nfe")

NS_NFE = "http://www.portalfiscal.inf.br/nfe"
NS_DS  = "http://www.w3.org/2000/09/xmldsig#"
NS_S11 = "http://schemas.xmlsoap.org/soap/envelope/"
NS_S12 = "http://www.w3.org/2003/05/soap-envelope"

# Distribuicao DF-e (ja validado em producao)
DIST_URL    = "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx"
NS_DIST     = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe"
DIST_ACTION = '"' + NS_DIST + '/nfeDistDFeInteresse"'

# Recepcao de Evento (Ambiente Nacional) - endpoint/acao descobertos via WSDL
NS_EVT = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4"
EVT_URLS = [
    "https://www.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
    "https://www1.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
]

_HTML = open(os.path.join(os.path.dirname(__file__), "templates", "index.html"),
             encoding="utf-8").read()
HTML = _HTML.replace("{{EMPRESA}}", EMPRESA).replace("{{SUBTITULO}}", SUBTITULO)


# ─── Certificado (carregado uma vez) ──────────────────────────────────────────
_CERT = {}

def certificado():
    if not _CERT:
        from cryptography.hazmat.primitives.serialization import (
            pkcs12, Encoding, PrivateFormat, NoEncryption)
        pk, cert, _ = pkcs12.load_key_and_certificates(
            base64.b64decode(CERT_B64), CERT_PASSWORD.encode())
        fc = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
        fc.write(cert.public_bytes(Encoding.PEM)); fc.close()
        fk = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
        fk.write(pk.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL,
                                  NoEncryption())); fk.close()
        _CERT.update(pk=pk, cert=cert, pem=(fc.name, fk.name))
    return _CERT


def envelope11(body_xml, prefixado=True):
    if prefixado:
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<soap:Envelope xmlns:soap="' + NS_S11 + '"><soap:Body>'
                + body_xml + '</soap:Body></soap:Envelope>')
    # Sem prefixo: nenhum namespace do envelope "vaza" para dentro do bloco
    # assinado, mantendo a canonicalizacao (C14N) identica a da assinatura.
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Envelope xmlns="' + NS_S11 + '"><Body>'
            + body_xml + '</Body></Envelope>')


def post_soap11(url, action, body_xml, prefixado=True):
    env = envelope11(body_xml, prefixado)
    r = req_lib.post(url, data=env.encode("utf-8"),
                     headers={"Content-Type": "text/xml; charset=utf-8",
                              "SOAPAction": action},
                     cert=certificado()["pem"], verify=False, timeout=25)
    return r


def soap_fault(root):
    f = root.find(".//{%s}Fault" % NS_S11)
    if f is None:
        f = root.find(".//{%s}Fault" % NS_S12)
    if f is None:
        return None
    return (f.findtext("faultstring")
            or f.findtext(".//{%s}Text" % NS_S12) or "SOAP Fault")


# ─── Distribuicao DF-e ────────────────────────────────────────────────────────
def consultar_dist(cnpj, chave):
    """Retorna (tipo, xml_str, cstat, motivo). tipo: 'completo' | 'resumo' | None"""
    inner = ('<distDFeInt versao="1.01" xmlns="' + NS_NFE + '">'
             '<tpAmb>' + AMBIENTE + '</tpAmb><cUFAutor>' + CUF + '</cUFAutor>'
             '<CNPJ>' + cnpj + '</CNPJ>'
             '<consChNFe><chNFe>' + chave + '</chNFe></consChNFe></distDFeInt>')
    body = ('<nfeDistDFeInteresse xmlns="' + NS_DIST + '"><nfeDadosMsg>'
            + inner + '</nfeDadosMsg></nfeDistDFeInteresse>')
    r = post_soap11(DIST_URL, DIST_ACTION, body)
    try:
        root = etree.fromstring(r.content)
    except etree.XMLSyntaxError:
        return None, None, "?", "Resposta invalida da SEFAZ (HTTP %s)" % r.status_code

    fault = soap_fault(root)
    if fault:
        return None, None, "fault", fault

    cstat  = root.findtext(".//{%s}cStat" % NS_NFE) or "?"
    motivo = root.findtext(".//{%s}xMotivo" % NS_NFE) or ""
    log.info("DistDFe CNPJ %s -> cStat=%s %s", cnpj, cstat, motivo)
    if cstat not in ("137", "138"):
        return None, None, cstat, motivo

    resumo = None
    for doc in root.iter("{%s}docZip" % NS_NFE):
        schema = doc.get("schema", "")
        try:
            xml = gzip.decompress(base64.b64decode(doc.text)).decode("utf-8")
        except Exception:
            continue
        log.info("docZip schema=%s", schema)
        if schema.startswith("procNFe"):
            return "completo", xml, cstat, motivo
        if schema.startswith("resNFe"):
            resumo = xml
    if resumo:
        return "resumo", resumo, cstat, motivo
    return None, None, cstat, motivo


# ─── Evento Ciencia da Operacao (210210) ──────────────────────────────────────
_EVT_INFO = {}

def descobrir_evento():
    """Le o WSDL do servico de evento para obter URL, SOAPAction e formato."""
    if _EVT_INFO:
        return _EVT_INFO
    for base in EVT_URLS:
        try:
            r = req_lib.get(base + "?wsdl", cert=certificado()["pem"],
                            verify=False, timeout=20)
        except Exception as e:
            log.warning("WSDL %s erro %s", base, e)
            continue
        log.info("WSDL %s -> HTTP %s", base, r.status_code)
        if r.status_code != 200:
            continue
        actions = re.findall(r'soapAction="([^"]+)"', r.text)
        ops = re.findall(r'<wsdl:operation name="([^"]+)"', r.text)
        elems = re.findall(r'<s:element name="([^"]+)"', r.text)
        op = ops[0] if ops else "nfeRecepcaoEvento"
        _EVT_INFO.update(
            url=base,
            action=actions[0] if actions else NS_EVT + "/" + op,
            operacao=op,
            embrulhado=op in elems,   # corpo envolto no elemento da operacao
            elementos=elems,
        )
        log.info("Evento descoberto: %s", _EVT_INFO)
        return _EVT_INFO
    return _EVT_INFO


def assinar(evento_el, inf_el):
    c = certificado()
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    digest = base64.b64encode(
        hashlib.sha1(etree.tostring(inf_el, method="c14n")).digest()).decode()

    S = lambda tag: "{%s}%s" % (NS_DS, tag)
    sig = etree.SubElement(evento_el, S("Signature"), nsmap={None: NS_DS})
    si = etree.SubElement(sig, S("SignedInfo"))
    etree.SubElement(si, S("CanonicalizationMethod"),
                     Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    etree.SubElement(si, S("SignatureMethod"),
                     Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1")
    ref = etree.SubElement(si, S("Reference"), URI="#" + inf_el.get("Id"))
    trs = etree.SubElement(ref, S("Transforms"))
    etree.SubElement(trs, S("Transform"),
                     Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature")
    etree.SubElement(trs, S("Transform"),
                     Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    etree.SubElement(ref, S("DigestMethod"),
                     Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    etree.SubElement(ref, S("DigestValue")).text = digest

    valor = c["pk"].sign(etree.tostring(si, method="c14n"),
                         padding.PKCS1v15(), hashes.SHA1())
    etree.SubElement(sig, S("SignatureValue")).text = base64.b64encode(valor).decode()
    ki = etree.SubElement(sig, S("KeyInfo"))
    xd = etree.SubElement(ki, S("X509Data"))
    etree.SubElement(xd, S("X509Certificate")).text = base64.b64encode(
        c["cert"].public_bytes(serialization.Encoding.DER)).decode()


def montar_env_evento(cnpj, chave):
    """Monta o lote com 1 evento de Ciencia da Operacao.
    O <evento> e criado e assinado como documento proprio, para que o
    namespace da NF-e fique declarado na propria tag <evento>. A SEFAZ
    recorta cada <evento> do lote para validar; sem essa declaracao o
    hash muda e a assinatura e rejeitada (cStat 297)."""
    N = lambda tag: "{%s}%s" % (NS_NFE, tag)
    evento = etree.Element(N("evento"), nsmap={None: NS_NFE}, versao="1.00")
    inf = etree.SubElement(evento, N("infEvento"), Id="ID210210" + chave + "01")
    dh = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))
          - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    for tag, val in [("cOrgao", "91"), ("tpAmb", AMBIENTE), ("CNPJ", cnpj),
                     ("chNFe", chave), ("dhEvento", dh), ("tpEvento", "210210"),
                     ("nSeqEvento", "1"), ("verEvento", "1.00")]:
        etree.SubElement(inf, N(tag)).text = val
    det = etree.SubElement(inf, N("detEvento"), versao="1.00")
    etree.SubElement(det, N("descEvento")).text = "Ciencia da Operacao"
    assinar(evento, inf)
    evento_xml = etree.tostring(evento, encoding="unicode")
    id_lote = str(int(time.time()))[-15:]
    return ('<envEvento xmlns="' + NS_NFE + '" versao="1.00">'
            '<idLote>' + id_lote + '</idLote>' + evento_xml + '</envEvento>')


def registrar_ciencia(cnpj, chave):
    """Retorna (ok, mensagem)."""
    info = descobrir_evento()
    if not info:
        return False, "Servico de eventos da SEFAZ indisponivel no momento."
    env_xml = montar_env_evento(cnpj, chave)
    dados = '<nfeDadosMsg xmlns="' + NS_EVT + '">' + env_xml + '</nfeDadosMsg>'
    variantes = [dados]
    alt = ('<' + info["operacao"] + ' xmlns="' + NS_EVT + '">'
           '<nfeDadosMsg>' + env_xml + '</nfeDadosMsg></' + info["operacao"] + '>')
    variantes = [alt, dados] if info.get("embrulhado") else [dados, alt]

    ultimo = ""
    for corpo in variantes:
        r = post_soap11(info["url"], '"' + info["action"] + '"', corpo, prefixado=False)
        log.info("Evento HTTP %s: %s", r.status_code, r.text[:1500])
        try:
            root = etree.fromstring(r.content)
        except etree.XMLSyntaxError:
            ultimo = "Resposta invalida (HTTP %s)" % r.status_code
            continue
        fault = soap_fault(root)
        if fault:
            ultimo = fault
            continue
        lote = root.findtext(".//{%s}retEnvEvento/{%s}cStat" % (NS_NFE, NS_NFE))
        ret = root.find(".//{%s}retEvento/{%s}infEvento" % (NS_NFE, NS_NFE))
        if ret is None:
            mot = root.findtext(".//{%s}xMotivo" % NS_NFE) or ""
            return False, "Lote cStat=%s %s" % (lote, mot)
        cst = ret.findtext("{%s}cStat" % NS_NFE)
        mot = ret.findtext("{%s}xMotivo" % NS_NFE) or ""
        log.info("Ciencia chave %s -> cStat=%s %s", chave, cst, mot)
        # 135 registrado | 136 registrado s/ vinculo | 573 ja existente
        if cst in ("135", "136", "573"):
            return True, mot
        return False, "cStat=%s %s" % (cst, mot)
    return False, ultimo or "Falha ao registrar ciencia."


# ─── Rotas ────────────────────────────────────────────────────────────────────
def resposta_xml(xml, chave):
    buf = io.BytesIO(xml.encode("utf-8")); buf.seek(0)
    return send_file(buf, mimetype="application/xml", as_attachment=True,
                     download_name="NF-e_" + chave + ".xml")


@app.route("/")
def index():
    return HTML


@app.route("/consultar", methods=["POST"])
def consultar():
    data = request.get_json(force=True)
    chave = re.sub(r"\D", "", data.get("chave", ""))
    if len(chave) != 44:
        return jsonify({"erro": "Chave de acesso invalida. Deve ter 44 digitos."}), 400
    if not CERT_B64 or not CNPJS:
        return jsonify({"erro": "Servidor sem certificado/CNPJ configurado."}), 500

    try:
        for cnpj in CNPJS:
            tipo, xml, cstat, motivo = consultar_dist(cnpj, chave)
            if tipo == "completo":
                return resposta_xml(xml, chave)
            if tipo != "resumo":
                continue

            # So veio o resumo: registra ciencia e tenta de novo
            ok, msg = registrar_ciencia(cnpj, chave)
            if not ok:
                return jsonify({"erro": "A SEFAZ liberou so o resumo da nota e nao foi "
                                        "possivel registrar a Ciencia da Operacao: " + msg}), 502
            for espera in (4, 8):
                time.sleep(espera)
                tipo, xml, _, _ = consultar_dist(cnpj, chave)
                if tipo == "completo":
                    return resposta_xml(xml, chave)
            return jsonify({"erro": "Ciencia da Operacao registrada. A SEFAZ ainda esta "
                                    "liberando o XML completo - tente novamente em "
                                    "alguns minutos."}), 409

        return jsonify({"erro": "NF-e nao encontrada para este CNPJ. Verifique a chave."}), 404
    except Exception as e:
        log.exception("Erro")
        return jsonify({"erro": str(e)}), 500


@app.route("/diagnostico")
def diagnostico():
    """Mostra o que o servidor descobriu do servico de eventos (sem dados sensiveis)."""
    info = dict(descobrir_evento())
    info.pop("elementos", None)
    return jsonify({"evento": info or "nao encontrado", "cnpjs": len(CNPJS),
                    "certificado": bool(CERT_B64)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
