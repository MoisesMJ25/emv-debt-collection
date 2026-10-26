"""Notificaciones: correo con totales y casos críticos, y Teams sin datos personales."""

from __future__ import annotations

import html
import logging

from emv_debt_collection.config import Config
from emv_debt_collection.domain.models import DebtResult, Outcome, RunReport
from emv_debt_collection.ports import SendEmail, SendMessage
from emv_debt_collection.shared import clp, describe

log = logging.getLogger(__name__)

ICON = {"SUCCESS": "✅", "PARTIAL": "⚠️", "FAILED": "❌"}
LABEL = {"SUCCESS": "Exitoso", "PARTIAL": "Parcialmente exitoso", "FAILED": "Con errores"}
OUTCOME_LABEL = {
    Outcome.PLANNED: "Se cobraría",
    Outcome.CHARGED: "Saldada",
    Outcome.PARTIALLY_CHARGED: "Cobro parcial",
    Outcome.ALREADY_CHARGED: "Ya saldada",
    Outcome.NOT_ENOUGH_FUNDS: "Sin saldo suficiente",
    Outcome.EXCLUDED: "Excluida (revisión)",
    Outcome.FAILED: "Fallida",
}
LOGO_URL = "https://metromuv.cl/wp-content/themes/metromuv/assets/img/logo.svg"


def _summary_items(r: RunReport, cfg: Config) -> list[tuple[str, str]]:
    """Resumen común a correo y Teams: un solo lugar define qué cifras se informan."""
    items = [
        ("Fecha de corte", str(r.run_date or "-")),
        ("Deudas leídas", str(r.debts_read)),
        ("Cuentas con deuda", str(r.accounts_total)),
        ("Cuentas con saldo", str(r.accounts_candidates)),
        ("Monto cobrado", f"{clp(r.charged_amount)} en {r.charged_count} cobros"),
        ("Deudas saldadas", str(r.count(Outcome.CHARGED))),
        ("Deudas con cobro parcial", str(r.count(Outcome.PARTIALLY_CHARGED))),
        ("Ya saldadas anteriormente", str(r.count(Outcome.ALREADY_CHARGED))),
        ("Excluidas (revisión manual)", str(r.count(Outcome.EXCLUDED))),
        ("Fallidas", str(r.count(Outcome.FAILED))),
    ]
    if cfg.dry_run or r.count(Outcome.PLANNED):
        items.append(("Planificadas (no ejecutadas)", f"{r.count(Outcome.PLANNED)} ({clp(r.planned_amount)})"))
    if r.reblock_failures:
        items.append(("⚠️ CUENTAS QUE QUEDARON DESBLOQUEADAS", str(r.reblock_failures)))
    if r.overcharged:
        items.append(("⚠️ DEUDAS CON POSIBLE SOBRECOBRO", str(r.overcharged)))
    return items


def build_teams_message(r: RunReport, cfg: Config) -> str:
    e = html.escape
    items = "".join(f"<li><b>{e(k)}:</b> {e(v)}</li>" for k, v in _summary_items(r, cfg))
    extra = f"<li><b>Error general:</b> {e(r.fatal_error[:500])}</li>" if r.fatal_error else ""

    return (
        f"<p><b>{ICON[r.status]} Cobro deuda EMV | {LABEL[r.status]} {cfg.env if cfg.env == 'dev' else ''}"
        f"{' (DRY-RUN)' if cfg.dry_run else ''}</b></p><ul>{items}{extra}</ul><p><b>run_id:</b> {r.run_id}</p>"
    )


def _critical_rows(r: RunReport) -> list[DebtResult]:
    """Casos que requieren acción inmediata y no deben esperar una consulta a Delta:
    cuentas que quedaron desbloqueadas, cobros en estado incierto y posibles sobrecobros.
    Normalmente la lista está vacía."""
    return [x for x in r.results if x.reblock_failed or x.uncertain or x.overcharged]


def _critical_label(x: DebtResult) -> str:
    if x.reblock_failed:
        return "Cuenta DESBLOQUEADA"
    return "Posible sobrecobro" if x.overcharged else "Cobro incierto"


def build_email(r: RunReport, cfg: Config) -> tuple[str, str]:
    """Solo totales: el detalle por deuda vive en la tabla de resultados (con miles de filas por
    ejecución, un correo con el detalle no se puede leer). Excepción: los casos críticos."""
    e = html.escape
    subject = (
        f"{'[DRY-RUN] ' if cfg.dry_run else ''}{ICON[r.status]} Cobro deuda EMV "
        f"{LABEL[r.status].lower()} - {r.run_date or ''}"
    )
    summary = "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in _summary_items(r, cfg))

    critical = _critical_rows(r)
    critical_html = ""
    if critical:
        rows = "".join(
            f"<tr><td>{e(x.debt.account_id)}</td><td>{e(x.debt.transaction_id)}</td><td>{clp(x.debt.amount)}</td>"
            f"<td>{_critical_label(x)}</td><td>{e(x.detail)}</td></tr>"
            for x in critical
        )
        critical_html = f"""<h4>⚠️ Requiere acción inmediata</h4>
        <table border="1" cellpadding="4" cellspacing="0" style="border-collapse: collapse;">
          <tr><th>Cuenta</th><th>Transacción</th><th>Deuda</th><th>Problema</th><th>Detalle</th></tr>{rows}
        </table>"""

    if cfg.dry_run:
        detail_html = "<p>Dry-run: el detalle por deuda está en la salida del notebook (no se guarda en Delta).</p>"
    else:
        query = f"SELECT * FROM {cfg.results_table} WHERE run_id = '{r.run_id}'"  # noqa: S608 (texto informativo)
        detail_html = f"<p>Detalle por deuda en tabla:</p><pre>{e(query)}</pre>"

    body = f"""<html><body>
        <h4>Resumen del cobro de deuda EMV [{cfg.env}]{" (DRY-RUN, sin cobros reales)" if cfg.dry_run else ""}</h4>
        <table border="1" cellpadding="4" cellspacing="0" style="border-collapse: collapse;">
          <tr><td>Estado</td><td><b>{LABEL[r.status]}</b></td></tr>{summary}
          <tr><td>run_id</td><td>{r.run_id}</td></tr>
        </table>
        {f"<p><b>Error general:</b> {e(r.fatal_error)}</p>" if r.fatal_error else ""}
        {"".join(f"<p><b>Advertencia:</b> {e(w)}</p>" for w in r.warnings)}
        {critical_html}
        {detail_html if r.results else "<p>No había deudas pendientes para la fecha de corte.</p>"}
        <br><br>Saludos,<br>Equipo de Operaciones<br><br>
        <img src='{LOGO_URL}' width='150' alt='Logo MetroMUV'/>
        </body></html>"""
    return subject, body


def _send_emails(send_email: SendEmail, recipients: tuple[str, ...], subject: str, body: str) -> None:
    for email in recipients:
        try:
            send_email(email, subject, body)
        except Exception as exc:
            log.error("Fallo al enviar correo a %s: %s", email, exc)


class Notifier:
    """Adaptador RunNotifier sobre `utils.send_email` y `utils.send_message` (inyectados)."""

    def __init__(self, cfg: Config, send_email: SendEmail, send_message: SendMessage):
        self.cfg = cfg
        self._send_email = send_email
        self._send_message = send_message

    def notify(self, report: RunReport) -> None:
        """Un fallo del correo se registra y sigue. Un fallo de Teams queda en el reporte: deja el job en rojo."""
        try:
            _send_emails(self._send_email, self.cfg.email_recipients, *build_email(report, self.cfg))
        except Exception as exc:
            log.error("Fallo al construir el correo: %s", exc)
        try:
            self._send_message(build_teams_message(report, self.cfg), self.cfg.teams_chat_id)
        except Exception as exc:
            log.error("Fallo al enviar notificación Teams: %s", exc)
            report.teams_error = describe(exc)
