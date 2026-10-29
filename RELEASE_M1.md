# QuantLab — Release M1 local

**Estado:** F4 implementada. A resolução do lock, a checagem focal de P&L e o smoke Streamlit de navegação/leitura de runs passaram. O checklist manual completo está em `docs/MANUAL_QA.md`; a navegação manual de fetch/import fica para a execução do proprietário.

## O que funciona

- Painel Streamlit com Dados, Backtests, Operações e Runs anteriores.
- Demo sintética offline; importação do ZIP mensal oficial; plano de fetch com confirmação explícita; QA e criação explícita de snapshot.
- Backtests F3 spot long-only com Cash, Buy & hold, SMA ou Breakout, parâmetros/custos configuráveis e artefatos HTML/JSON/CSV.
- Equity, benchmark BUY_HOLD, drawdown visual, tabela de operações com timestamps, preços, quantidade, taxa, veto/motivo e P&L realizado de trades fechados.
- Histórico local de runs, proveniência do snapshot/política, download de relatório/JSON/CSV e persistência ao reabrir.
- Erros resumidos no painel; logs técnicos em `state/streamlit.log`.

## Comandos

```powershell
uv sync
uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1
```

Para operar pelo CLI, siga também os exemplos F1–F3 do `README.md`. Ingestão remota continua condicionada à confirmação na tela.

## Limitações

- Fee e slippage são hipóteses, `available_at` é assumido e o fill no open 1m é proxy OHLCV; nenhuma curva comprova rentabilidade.
- Dados reais são `IN_SAMPLE`; dados da demo são sempre `DADOS SINTÉTICOS`/`DEMO`.
- A tabela marca P&L realizado na saída fechada; posições abertas têm apenas equity marcada.
- M1 é local e spot long-only. Não inclui F5/M2 (walk-forward/holdout/comparação), ML, paper replay/forward, derivativos, alavancagem ou corretora/ordens reais.
- Fetch remoto e a navegação manual completa precisam ser concluídos no checklist para declarar o aceite manual M1; o smoke automatizado não substitui esses cliques.
