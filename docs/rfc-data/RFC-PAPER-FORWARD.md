# RFC F8 — PAPER_FORWARD Binance Spot BTCUSDT

1. **Escopo:** um único subprojeto F8, isolado em `quantlab.paper_forward`; sem trocar a engine canônica.
2. **Hipótese:** um feed incremental público recente permite observar disponibilidade/frescor que backtests M1–M3 com arquivos históricos não medem.
3. **Benefício:** demonstrar em uma sessão local o caminho candle fechado → sinal disponível → trade elegível → ledger paper auditável.
4. **Universo:** Binance Spot, símbolo `BTCUSDT`, `instrument_id=BINANCE:SPOT:BTCUSDT`; apenas estratégia `BUY_HOLD` ou estado inicial cash.
5. **Fonte de mercado:** WebSocket público de produção `btcusdt@kline_1m` + `btcusdt@aggTrade` em `stream.binance.com:9443`.
6. **Relógio:** endpoint público `data-api.binance.vision/api/v3/time`; RTT acima de 2 s bloqueia a sessão; timestamps locais avançam por relógio monotônico.
7. **Licença:** retenção/redistribuição do market data está **NÃO VERIFICADA**; os [termos oficiais de produto Binance](https://developers.binance.com/en/docs/products/spot/PROD-TERMS-OF-USE) são referência, não uma conclusão jurídica de permissão.
8. **Temporalidade:** barras só entram quando `x=true`; a primeira barra elegível precisa começar após a ativação e ser observada inteira.
9. **Sinal/fill:** disponibilidade é `max(receipt UTC alinhado, event time) + delay positivo`; fill só usa aggTrade cujo tempo de origem e receipt são estritamente posteriores.
10. **Preço/custos:** aggTrade elegível fornece o preço de referência para `execute_market_target` F3; fee e slippage por lado são hipóteses explicitadas.
11. **Retificações:** updates em candle aberto são ignorados; candle fechado repetido, lacuna, reordenação ou salto na sequência aggTrade põem a sessão em `HALTED`.
12. **Reconnect:** desconexão, atraso/staleness, timestamp inválido ou falha de ledger fecham a sessão; reconexão automática não foi selecionada.
13. **Interfaces:** novo módulo F8, comando `quantlab paper-forward run` e página Streamlit Paper Forward; sem alterações no ledger F3 ou no fluxo offline.
14. **Risco:** drawdown máximo é verificado em cada aggTrade; feed sem mensagem por 45 s, gap, sequência quebrada ou ledger inválido bloqueiam a sessão.
15. **Artefatos:** cada sessão cria `run.json`, journal JSONL encadeado, `state.json` atômico e `report.html`, rotulados `PAPER_FORWARD`.
16. **Aceite:** smoke deve receber feed recente real, registrar candle fechado pós-ativação, um sinal, fill paper temporalmente posterior, fee única, saldo não negativo e journal verificável.
17. **Adiado:** outras venues/símbolos, estratégias com warm-up, comparação de fills entre venues, armazenamento histórico, reconexão/retificação automática, nível 2, perp, ML e ordens reais.
18. **Referência:** [documentação oficial de Binance Spot WebSocket Streams](https://developers.binance.com/en/docs/products/spot/web-socket-streams); usar feed WebSocket, nunca daily archive como fonte live.
