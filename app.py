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

VERSAO = "v5.2-confirmacao"

# ─── Configuracao ─────────────────────────────────────────────────────────────
CERT_B64      = os.environ.get("CERT_B64", "")
CERT_PASSWORD = os.environ.get("CERT_PASSWORD", "")
AMBIENTE      = os.environ.get("AMBIENTE", "1")
CUF           = os.environ.get("CUF", "16")
EMPRESA       = os.environ.get("EMPRESA", "Farmacia")
SUBTITULO     = os.environ.get("SUBTITULO", "Consulta NF-e")
CNPJS = [c.strip() for c in os.environ.get("CNPJS", "").split(",") if c.strip()]
# 1 = registra Ciencia da Operacao automaticamente quando so vier o resumo
CIENCIA_AUTOMATICA = os.environ.get("CIENCIA_AUTOMATICA", "1") == "1"
# 1 = permite registrar Confirmacao da Operacao (210200) para notas com mais
#     de 10 dias, sempre a pedido do usuario (botao na tela)
CONFIRMACAO_HABILITADA = os.environ.get("CONFIRMACAO_HABILITADA", "0") == "1"
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


def c14n(el):
    """C14N inclusiva de um trecho do documento.
    etree.tostring(el, method="c14n") em elementos internos gera xmlns=""
    espurios nos netos (defeito do lxml); serializar e reabrir o trecho como
    documento proprio produz a forma canonica correta."""
    return etree.tostring(etree.fromstring(etree.tostring(el)), method="c14n")


def assinar(evento_el, inf_el):
    c = certificado()
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    digest = base64.b64encode(
        hashlib.sha1(c14n(inf_el)).digest()).decode()

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

    valor = c["pk"].sign(c14n(si),
                         padding.PKCS1v15(), hashes.SHA1())
    etree.SubElement(sig, S("SignatureValue")).text = base64.b64encode(valor).decode()
    ki = etree.SubElement(sig, S("KeyInfo"))
    xd = etree.SubElement(ki, S("X509Data"))
    etree.SubElement(xd, S("X509Certificate")).text = base64.b64encode(
        c["cert"].public_bytes(serialization.Encoding.DER)).decode()


def montar_env_evento(cnpj, chave, tp="210210", desc="Ciencia da Operacao"):
    """Monta o lote com 1 evento de Ciencia da Operacao.
    O <evento> e criado e assinado como documento proprio, para que o
    namespace da NF-e fique declarado na propria tag <evento>. A SEFAZ
    recorta cada <evento> do lote para validar; sem essa declaracao o
    hash muda e a assinatura e rejeitada (cStat 297)."""
    N = lambda tag: "{%s}%s" % (NS_NFE, tag)
    evento = etree.Element(N("evento"), nsmap={None: NS_NFE}, versao="1.00")
    inf = etree.SubElement(evento, N("infEvento"), Id="ID" + tp + chave + "01")
    dh = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))
          - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    for tag, val in [("cOrgao", "91"), ("tpAmb", AMBIENTE), ("CNPJ", cnpj),
                     ("chNFe", chave), ("dhEvento", dh), ("tpEvento", tp),
                     ("nSeqEvento", "1"), ("verEvento", "1.00")]:
        etree.SubElement(inf, N(tag)).text = val
    det = etree.SubElement(inf, N("detEvento"), versao="1.00")
    etree.SubElement(det, N("descEvento")).text = desc
    assinar(evento, inf)
    evento_xml = etree.tostring(evento, encoding="unicode")
    id_lote = str(int(time.time()))[-15:]
    return ('<envEvento xmlns="' + NS_NFE + '" versao="1.00">'
            '<idLote>' + id_lote + '</idLote>' + evento_xml + '</envEvento>')


def registrar_ciencia(cnpj, chave):
    ok, cst, msg = registrar_evento(cnpj, chave)
    return ok, msg


def registrar_evento(cnpj, chave, tp="210210", desc="Ciencia da Operacao"):
    """Retorna (ok, mensagem)."""
    info = descobrir_evento()
    if not info:
        return False, None, "Servico de eventos da SEFAZ indisponivel no momento."
    env_xml = montar_env_evento(cnpj, chave, tp, desc)
    log.info("[%s] envEvento enviado: %s", VERSAO, env_xml[:900])
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
            return False, lote, "Lote cStat=%s %s" % (lote, mot)
        cst = ret.findtext("{%s}cStat" % NS_NFE)
        mot = ret.findtext("{%s}xMotivo" % NS_NFE) or ""
        log.info("Ciencia chave %s -> cStat=%s %s", chave, cst, mot)
        # 135 registrado | 136 registrado s/ vinculo | 573 ja existente
        if cst in ("135", "136", "573"):
            return True, cst, mot
        return False, cst, "cStat=%s %s" % (cst, mot)
    return False, None, ultimo or "Falha ao registrar o evento."


# ─── Rotas ────────────────────────────────────────────────────────────────────
def resposta_xml(xml, chave):
    buf = io.BytesIO(xml.encode("utf-8")); buf.seek(0)
    return send_file(buf, mimetype="application/xml", as_attachment=True,
                     download_name="NF-e_" + chave + ".xml")


@app.route("/")
def index():
    return HTML


# Ordem de tentativa: a filial que acertou por ultimo vai para o inicio.
# Reduz o numero de consultas por clique (mais rapido e menos risco de 656).
_ORDEM = list(CNPJS)

def _promover(cnpj):
    if cnpj in _ORDEM:
        _ORDEM.remove(cnpj)
    _ORDEM.insert(0, cnpj)


@app.route("/consultar", methods=["POST"])
def consultar():
    data = request.get_json(force=True)
    chave = re.sub(r"\D", "", data.get("chave", ""))
    if len(chave) != 44:
        return jsonify({"erro": "Chave de acesso invalida. Deve ter 44 digitos."}), 400
    if not CERT_B64 or not CNPJS:
        return jsonify({"erro": "Servidor sem certificado/CNPJ configurado."}), 500

    inicio = time.time()
    ocorrencias = {}          # cStat/erro -> quantidade (para a mensagem final)
    resumo_em = None          # CNPJ onde a nota existe mas so veio o resumo

    for cnpj in list(_ORDEM):
        if time.time() - inicio > 90:
            ocorrencias["tempo esgotado"] = ocorrencias.get("tempo esgotado", 0) + 1
            break
        try:
            tipo, xml, cstat, motivo = consultar_dist(cnpj, chave)
        except req_lib.exceptions.RequestException as e:
            log.warning("Falha de rede CNPJ %s: %s", cnpj, e)
            ocorrencias["sem resposta da SEFAZ"] = ocorrencias.get("sem resposta da SEFAZ", 0) + 1
            continue

        if tipo == "completo":
            _promover(cnpj)
            log.info("XML completo via CNPJ %s em %.1fs", cnpj, time.time() - inicio)
            return resposta_xml(xml, chave)
        if tipo == "resumo":
            _promover(cnpj)
            resumo_em = cnpj
            break
        if cstat == "217":
            # "NF-e nao consta na base": depende da chave, nao do CNPJ.
            # Consultar as outras filiais so gastaria tempo e cota da SEFAZ.
            log.info("cStat 217 para a chave %s - nota fora da base nacional", chave)
            return jsonify({"erro": "Esta nota ainda nao esta disponivel na base nacional da "
                                    "SEFAZ. Notas recem-emitidas podem levar de alguns minutos "
                                    "a algumas horas para aparecer. Confira se a chave esta "
                                    "correta e tente novamente mais tarde."}), 404
        if cstat == "656":
            # Consumo indevido: SEFAZ bloqueia por ~1h; nao adianta insistir
            return jsonify({"erro": "A SEFAZ bloqueou temporariamente as consultas por excesso "
                                    "de requisicoes (cStat 656). Aguarde cerca de 1 hora e "
                                    "tente novamente."}), 429
        ocorrencias[cstat] = ocorrencias.get(cstat, 0) + 1

    if resumo_em:
        if not CIENCIA_AUTOMATICA:
            return jsonify({"erro": "A nota existe, mas a SEFAZ so libera o XML completo apos a "
                                    "Ciencia da Operacao. Aguarde o sistema fiscal registrar a "
                                    "ciencia e tente novamente."}), 409
        ok, cst, msg = registrar_evento(resumo_em, chave)
        if not ok and cst == "596":
            if CONFIRMACAO_HABILITADA:
                return jsonify({"erro": "A SEFAZ nao aceita mais a Ciencia da Operacao para esta "
                                        "nota. Para baixar o XML original e preciso confirmar o "
                                        "recebimento da mercadoria.",
                                "acao": "confirmar"}), 409
            return jsonify({"erro": "Esta nota tem mais de 10 dias e a SEFAZ nao aceita mais a "
                                    "Ciencia da Operacao. Peca o XML ao fornecedor."}), 409
        if not ok:
            return jsonify({"erro": "A SEFAZ liberou so o resumo da nota e nao foi possivel "
                                    "registrar a Ciencia da Operacao: " + msg}), 502
        for espera in (4, 8):
            time.sleep(espera)
            try:
                tipo, xml, _, _ = consultar_dist(resumo_em, chave)
            except req_lib.exceptions.RequestException:
                continue
            if tipo == "completo":
                return resposta_xml(xml, chave)
        return jsonify({"erro": "Ciencia da Operacao registrada. A SEFAZ ainda esta liberando "
                                "o XML completo - tente novamente em alguns minutos."}), 409

    log.info("Nao encontrada. Ocorrencias: %s", ocorrencias)
    if "sem resposta da SEFAZ" in ocorrencias or "tempo esgotado" in ocorrencias:
        return jsonify({"erro": "A SEFAZ demorou ou nao respondeu. Tente novamente em "
                                "instantes."}), 504
    return jsonify({"erro": "NF-e nao localizada para nenhum CNPJ cadastrado. Confira a chave "
                            "e se a nota foi emitida para uma das lojas."}), 404


@app.route("/confirmar", methods=["POST"])
def confirmar():
    """Confirmacao da Operacao (210200), so a pedido explicito do usuario."""
    if not CONFIRMACAO_HABILITADA:
        return jsonify({"erro": "Confirmacao de recebimento nao habilitada neste sistema."}), 403
    chave = re.sub(r"\D", "", request.get_json(force=True).get("chave", ""))
    if len(chave) != 44:
        return jsonify({"erro": "Chave de acesso invalida."}), 400

    alvo = None
    for cnpj in list(_ORDEM):
        try:
            tipo, xml, cstat, _ = consultar_dist(cnpj, chave)
        except req_lib.exceptions.RequestException:
            continue
        if tipo == "completo":
            return resposta_xml(xml, chave)
        if tipo == "resumo":
            alvo = cnpj; break
        if cstat in ("217", "656"):
            break
    if not alvo:
        return jsonify({"erro": "Nota nao localizada para confirmacao."}), 404

    log.info("Confirmacao da Operacao solicitada: chave %s CNPJ %s", chave, alvo)
    ok, cst, msg = registrar_evento(alvo, chave, "210200", "Confirmacao da Operacao")
    if not ok:
        return jsonify({"erro": "Nao foi possivel confirmar o recebimento: " + msg}), 502
    for espera in (4, 8):
        time.sleep(espera)
        try:
            tipo, xml, _, _ = consultar_dist(alvo, chave)
        except req_lib.exceptions.RequestException:
            continue
        if tipo == "completo":
            return resposta_xml(xml, chave)
    return jsonify({"erro": "Recebimento confirmado. A SEFAZ ainda esta liberando o XML - "
                            "tente baixar novamente em alguns minutos."}), 409


@app.route("/diagnostico")
def diagnostico():
    """Mostra o que o servidor descobriu do servico de eventos (sem dados sensiveis)."""
    info = dict(descobrir_evento())
    info.pop("elementos", None)
    return jsonify({"versao": VERSAO, "evento": info or "nao encontrado",
                    "cnpjs": len(CNPJS), "certificado": bool(CERT_B64),
                    "ciencia_automatica": CIENCIA_AUTOMATICA,
                    "confirmacao": CONFIRMACAO_HABILITADA})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
