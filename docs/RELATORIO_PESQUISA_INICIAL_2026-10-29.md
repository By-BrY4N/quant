# Pesquisa quantitativa inicial — 29/10/2026

## Escopo e evidência

Esta é uma primeira rodada exploratória usando a arquitetura existente do QuantLab. Não é uma certificação de rentabilidade nem recomendação de operação. Os resultados abaixo foram calculados pelo motor F3 sobre snapshots reais F2 QA PASS. Nenhuma ordem real foi enviada.

A pesquisa técnica anexada recomenda Spot com candles de 1m como fonte canônica, estratégias de tendência/momentum/breakout/mean reversion, custos explícitos, avaliação cronológica, comparação contra buy-and-hold e um holdout posterior congelado. A rodada implementa esse recorte com os componentes F1/F2/F3/F5 existentes; não reconstrói o produto. O relatório recomenda também análises que não estão habilitadas nesta rodada, descritas nas limitações.

## Dados coletados e disponibilidade

A fonte foi Binance Public Data Spot, klines de 1m, para BTCUSDT e ETHUSDT.

- F1 contém 81 ZIPs mensais verificados por ativo, de 2020-01 a 2026-09, mais 28 ZIPs diários verificados por ativo, de 2026-10-01 a 2026-10-28.
- Total: 218 objetos raw verificados e 345.050.985 bytes de ZIPs. F1 validou CHECKSUM/SHA-256 e CRC. Arquivos raw são imutáveis e locais.
- F2 já cobria até 2026-06: 70 meses PASS e 8 QUARANTINED por símbolo. As quarentenas são 2020-02, 2020-03, 2020-12, 2021-02, 2021-04, 2021-08, 2021-12 e 2023-03. Nenhuma lacuna foi preenchida.
- O trecho gapless usado nos testes começa em 2023-04. As partições avaliadas nos snapshots utilizados pelos backtests reportaram zero minutos faltantes.
- Os arquivos mensais de 2026-07 a 2026-09 e os diários de outubro foram baixados em F1, mas ainda não passaram pela auditoria F2. Não são elegíveis para backtest e não foram pontuados.
- Há partições QA PASS de 2026-04 a 2026-06, mas não foram incluídas nas métricas desta rodada.

Downloads adicionais executados:

~~~powershell
uv run quantlab data fetch --symbol BTCUSDT --granularity monthly --start 2026-07 --end 2026-09 --max-transfer-mb 512
uv run quantlab data fetch --symbol ETHUSDT --granularity monthly --start 2026-07 --end 2026-09 --max-transfer-mb 512
uv run quantlab data fetch --symbol BTCUSDT --granularity daily --start 2026-10-01 --end 2026-10-28 --max-transfer-mb 512
uv run quantlab data fetch --symbol ETHUSDT --granularity daily --start 2026-10-01 --end 2026-10-28 --max-transfer-mb 512
~~~

A identidade de arquivo diário foi corrigida para validar datas fechadas do mês corrente com a regra diária. Uma regra mensal estava rejeitando esses arquivos apesar de o dia já estar fechado. A regressão focal passou.

## O que a plataforma já oferece

- F1 busca e verifica ZIPs oficiais Spot mensais e diários para BTCUSDT/ETHUSDT, com limites explícitos e raw imutável.
- F2 normaliza, aplica QA e fixa snapshots imutáveis por IDs exatos de partições; janelas com lacunas ou sobreposições não são preenchidas silenciosamente.
- F3 simula Spot long-only, caixa, buy-and-hold, cruzamento SMA, breakout, momentum e mean reversion nos timeframes 15m, 1h, 4h e 1d. Sinais usam candles fechados; fill usa o primeiro open 1m estritamente depois da disponibilidade; não há short, alavancagem, margem nem ordens reais.
- F5 registra configuração/hash, folds walk-forward cronológicos, resultados por fold, estresse de fee/slippage 2x/3x, critério primário, congelamento e acesso único ao holdout. O estado CANDIDATO_A_PAPER é apenas uma classificação de pesquisa.

## Desenho da rodada

Capital de referência: 1.000 USDT. Política comum: fee assumida de 10 bps por lado, slippage adverso proxy de 5 bps por lado, atraso de disponibilidade assumido de 1.000 ms, long-only e exposição máxima de 100%. Uma entrada seguida de uma saída representa 30 bps de fee/slippage nominais. Não há spread separado, e taxas não foram confirmadas para uma conta específica.

Estratégias e parâmetros principais definidos para esta comparação:

| Família | Regra | Timeframe | Parâmetros principais |
|---|---|---:|---|
| Baselines | Cash e buy-and-hold | todos | sem parâmetros |
| Tendência | cruzamento SMA | 4h | rápida 20, lenta 50 |
| Momentum | retorno positivo contra close de 24 barras atrás | 1d | lookback 24 |
| Breakout | close acima da máxima anterior | 15m | lookback 20 |
| Mean reversion | z-score trailing de closes, long-only com histerese | 1h | janela 30, entrada ≤ -2, saída ≥ 0 |

Períodos cronológicos usados:

| Papel | Período UTC | Snapshot BTC | Snapshot ETH |
|---|---|---|---|
| Treino | 2023-04-01 a 2024-04-01 | snapshot-6128f04036bd63e5480e52ca49e0db99 | snapshot-30a78dfb1d2767ec23e9439ad9d15b59 |
| Validação | 2024-04-01 a 2025-04-01 | snapshot-bc7c887114286bbf53145ad084891167 | snapshot-11194982446605bc8ee412db4f6724e4 |
| Teste reservado | 2025-04-01 a 2026-04-01 | snapshot-d596ad2bac7d44b6bdfd2c4c09b661eb | snapshot-563dc5c5ee929bb2e26dbb6a60e433ff |
| Holdout final candidato | 2026-07-01 a 2026-10-29 | raw F1; sem snapshot QA | raw F1; sem snapshot QA |

O recorte de teste 2025-04 a 2026-04 não foi pontuado. A versão F5 de momentum de 24 barras terminou NO_GO no desenvolvimento ampliado; por isso não foi congelada nem autorizada para abertura do teste. O holdout final de julho a outubro está somente em raw F1 e permanece sem F2, sem congelamento e sem acesso.

## Comparação calculada

Cada linha compara a estratégia ao buy-and-hold dentro do mesmo ativo e período. Retorno, Sharpe, drawdown e exposição são percentuais/razões de equity marcadas a mercado. Win rate e profit factor consideram somente trades fechados e P&L líquido; “—” indica que a métrica não se aplica ou é indefinida. B&H permanece com posição aberta no final e por isso não tem trades fechados. O baseline Cash terminou em 0% de retorno, 0% de exposição e sem Sharpe definido em todas as janelas.

| Estratégia / período | Retorno líquido | B&H líquido | Sharpe diário | DD máx. | Win rate | Profit factor | Exposição | Trades fechados | Run ID |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| SMA 20/50 — BTC treino | 74,40% | 149,91% | 1,797 | 19,56% | 36,4% | 2,79 | 55,0% | 22 | 20261029T141945Z-8dd18177 |
| Momentum 24 — BTC treino | 151,20% | 149,91% | 2,562 | 17,51% | 50,0% | 3,54 | 56,3% | 10 | 20261029T142829Z-86a4a7ae |
| Breakout 20 — BTC treino | -96,07% | 149,91% | -18,577 | 96,10% | 11,5% | 0,16 | 4,7% | 1.160 | 20261029T142912Z-e1e77c35 |
| Mean reversion 30 — BTC treino | -4,63% | 149,91% | -0,124 | 17,42% | 68,3% | 0,92 | 23,8% | 123 | 20261029T142951Z-8ddd5a9d |
| SMA 20/50 — ETH treino | 50,92% | 99,74% | 1,303 | 37,58% | 39,1% | 2,18 | 54,9% | 23 | 20261029T143033Z-0e9e5501 |
| Momentum 24 — ETH treino | 29,32% | 99,74% | 0,821 | 27,13% | 28,6% | 1,72 | 54,4% | 21 | 20261029T143111Z-26f2722d |
| Breakout 20 — ETH treino | -97,08% | 99,74% | -19,331 | 97,09% | 11,3% | 0,17 | 4,7% | 1.161 | 20261029T143152Z-fe38e982 |
| Mean reversion 30 — ETH treino | -5,67% | 99,74% | -0,110 | 23,88% | 67,2% | 0,92 | 24,9% | 116 | 20261029T143233Z-20631ea2 |
| SMA 20/50 — BTC validação | -22,73% | 15,79% | -0,561 | 33,30% | 33,3% | 0,71 | 52,0% | 27 | 20261029T144307Z-41d40089 |
| Momentum 24 — BTC validação | -11,26% | 15,79% | -0,205 | 35,03% | 14,3% | 0,74 | 49,0% | 14 | 20261029T144345Z-29520e7f |
| Breakout 20 — BTC validação | -97,88% | 15,79% | -20,978 | 97,88% | 11,6% | 0,11 | 5,1% | 1.246 | 20261029T144425Z-29c6c72d |
| Mean reversion 30 — BTC validação | -26,00% | 15,79% | -1,084 | 33,66% | 62,3% | 0,70 | 28,1% | 122 | 20261029T144503Z-29f295d1 |
| Momentum 24 — ETH validação | -12,60% | -50,04% | -0,245 | 35,51% | 33,3% | 0,63 | 32,6% | 12 | 20261029T144545Z-a6467023 |

Parâmetros alternativos executados somente no treino BTC, sem seleção pelo período de validação:

| Família e configuração | Retorno líquido | B&H líquido | Sharpe diário | DD máx. | Win rate | Profit factor | Exposição | Trades fechados | Run ID |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| SMA 40/100 | 46,56% | 149,91% | 1,221 | 27,68% | 20,0% | 2,06 | 56,9% | 15 | 20261029T143910Z-a7e03ef8 |
| Momentum 48 | 137,69% | 149,91% | 2,482 | 17,52% | 60,0% | 15,88 | 59,3% | 5 | 20261029T143949Z-388916ab |
| Breakout 50 | -82,92% | 149,91% | -11,567 | 82,94% | 15,5% | 0,21 | 2,5% | 613 | 20261029T144028Z-cb166dfa |
| Mean reversion 45 | 7,77% | 149,91% | 0,449 | 17,65% | 72,2% | 1,14 | 23,2% | 90 | 20261029T144107Z-479d3fb5 |

Os retornos nos dois anos BTC de treino/validação não se repetem. No treino, momentum24 foi ligeiramente acima de B&H, enquanto a configuração perdeu 11,26% na validação contra +15,79% de B&H. O resultado de ETH momentum na validação foi -12,60%, melhor que B&H a -50,04%, mas ainda negativo em termos absolutos. A semelhança de configuração não implica portabilidade entre os dois ativos.

O breakout foi fortemente negativo nos dois ativos e períodos medidos; os custos de giro são elevados: na configuração BTC lookback20 no treino, fees de 685,33 USDT e slippage proxy de 342,66 USDT, contra retorno líquido de -96,07%. A variante lookback50 reduziu as transações de 1.160 para 613, mas ainda perdeu 82,92%.

Mean reversion 45 mudou de -4,63% para +7,77% no treino BTC em relação à janela 30, mas ficou muito abaixo de B&H. Os dois resultados mostram sensibilidade a parâmetros, sem evidência independente para escolher 45. Win rate elevado, por si só, não evitou perdas em mean reversion 30.

## Walk-forward e critérios F5

O F5 existente foi usado em folds cronológicos, sem shuffle, com carteira reiniciada em dinheiro no começo de cada janela. O critério primário, declarado no contrato de cada versão, foi Sharpe diário mediano dos folds de teste maior ou igual a zero; custos foram estressados em 2x e 3x. Os relatórios integrais de cada trial persistem localmente em artifacts/research.

| Família / parâmetros | Folds de teste: retorno líquido e Sharpe | B&H retorno por fold | Retorno sob stress 2x / 3x | Critério / estado |
|---|---|---|---|---|
| BTC SMA 20/50, 4h; 90d treino / 60d teste | -5,53% (Sharpe -2,21); +8,53% (1,57) | -14,34%; +29,54% | -7,21% / -8,87%; +7,08% / +5,64% | Mediana Sharpe -0,320; NO_GO |
| BTC breakout 20, 15m; 90d / 60d | -43,28% (-25,84); -33,54% (-11,04) | -14,34%; +29,54% | -67,05% / -80,85%; -60,86% / -76,95% | Mediana -18,440; NO_GO |
| BTC momentum 24, 1d; 90d / 60d | -1,29% (-0,43); +28,22% (4,59) | -14,34%; +29,54% | -1,59% / -1,88%; +27,26% / +26,31% | Mediana 2,081; CANDIDATO_A_PAPER |
| BTC momentum 48, 1d; 90d / 60d | -2,47% (-0,69); +21,83% (3,96) | -14,34%; +29,54% | -2,77% / -3,06%; +21,65% / +21,47% | Mediana 1,638; CANDIDATO_A_PAPER |
| BTC mean reversion 30, 1h; 90d / 60d | -1,08% (-0,21); -2,33% (-0,89) | -14,34%; +29,54% | -9,87% / -17,87%; -7,04% / -11,53% | Mediana -0,550; NO_GO |
| BTC momentum 24, 1d, desenvolvimento 24 meses; 365d / 180d | -39,03% (-3,23); +22,04% (1,25) | -6,52%; +33,92% | -40,74% / -42,41%; +19,68% / +17,37% | Mediana -0,990; NO_GO |

O último experimento ligou um snapshot QA PASS que contém o contexto 2024-04 a 2026-03, mas avaliaria formalmente somente 2025-04 a 2026-04. O contrato terminou NO_GO; o snapshot permaneceu selado, não foi congelado e não teve score de teste. O período de desenvolvimento ampliado contém o ano de validação que já aparece na tabela direta, portanto esse walk-forward ampliado é diagnóstico retrospectivo, não uma validação independente.

O momentum24 de um ano passa a regra F5 porque sua mediana de dois folds é positiva; contudo, um fold foi negativo, o outro concentrou quase todo o ganho, a versão de 48 barras também passou com retorno/Sharpe inferiores, e as verificações de validação foram negativas. Por prudência, nenhuma versão foi congelada nem promovida para teste. O status de candidato F5 não equivale a edge ou aprovação para capital real.

## Estabilidade por período, ativo, parâmetros e regime

- Período: defaults BTC mudaram de positivos em treino para negativos em validação; momentum tem essa inversão apesar dos dois folds de um ano mostrarem um segmento bom e outro ruim.
- Ativo: ETH treino teve retornos positivos em SMA e momentum, mas abaixo de B&H. Momentum ETH na validação caiu menos que B&H, mas também perdeu capital. Não há validação completa em ETH para as outras três famílias nesta rodada.
- Parâmetros: as quatro variantes BTC treinadas foram registradas como sensibilidade exploratória. Breakout continuou profundamente negativo, SMA reduziu retorno, momentum24/48 teve resultados próximos com amostras pequenas, e mean reversion mudou de sinal entre 30/45.
- Regime: não há classificador de regimes implementado nesta pesquisa. Os folds walk-forward atravessam janelas em que o B&H BTC ficou negativo e positivo; esses rótulos vêm do resultado observado do benchmark, não de uma classificação causal congelada. A dispersão dos resultados é evidência de instabilidade temporal, não explicação de regime.
- Seleção: as métricas anuais de treino foram vistas antes dos contratos F5 de um ano; os folds F5 são diagnósticos e não foram totalmente pré-registrados antes de qualquer resultado. A validação F3 manteve os parâmetros principais originais; nenhum parâmetro foi escolhido olhando o teste reservado.

## Convenções e limitações

- Sharpe é anualizado por sqrt(365), sobre retornos simples diários UTC de dias completos consecutivos, desvio padrão amostral e taxa livre diária zero. Gaps quebram a série; o número de observações fica nos JSONs.
- Win rate e profit factor usam P&L líquido de round trips fechados. B&H normalmente termina com posição aberta, então win rate e profit factor ficam indefinidos.
- Exposição e retorno são marcados a mercado. Uma posição aberta não é liquidada no final e não recebe custo hipotético de saída. B&H paga custos da entrada.
- Taxa, slippage e atraso são premissas. OHLCV e open 1m não provam execução; sem spread histórico, impacto ou profundidade.
- As taxas padrão aplicadas são hipótese de pesquisa, não taxa oficial Binance de uma conta.
- São poucos ativos/períodos e poucos parâmetros. Não foi executado bootstrap, DSR, PBO, Reality Check, análise de concentração de lucro ou teste formal de capacidade.
- Não foram abertos o período de teste 2025-04 a 2026-04 nem o holdout candidato 2026-07 a 2026-10. Os resultados não demonstram lucratividade futura.
- Os JSON/HTML/CSV e a base SQLite de pesquisa são locais e ignorados pelo Git; o relatório Markdown versionado contém as métricas calculadas, os run IDs e os estados dos trials.

## Comandos CLI dos 17 resultados F3

Todos os comandos abaixo usaram fee 10 bps/lado, slippage 5 bps/lado e 1.000 USDT. O primeiro run BTC momentum24 gravou as métricas, mas o processo falhou ao imprimir o símbolo √ no console Windows; após substituir esse caractere por texto ASCII, a mesma configuração concluiu com exit code 0. A linha consta uma vez na tabela de resultados.

~~~powershell
uv run quantlab backtest --snapshot snapshot-6128f04036bd63e5480e52ca49e0db99 --strategy sma_cross --timeframe 4h --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --sma-fast 20 --sma-slow 50 --output artifacts/research/initial/quick-check
uv run quantlab backtest --snapshot snapshot-6128f04036bd63e5480e52ca49e0db99 --strategy momentum --timeframe 1d --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --momentum-lookback 24 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-6128f04036bd63e5480e52ca49e0db99 --strategy breakout --timeframe 15m --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --breakout-lookback 20 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-6128f04036bd63e5480e52ca49e0db99 --strategy mean_reversion --timeframe 1h --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --mean-reversion-lookback 30 --mean-reversion-entry-z -2 --mean-reversion-exit-z 0 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-30a78dfb1d2767ec23e9439ad9d15b59 --strategy sma_cross --timeframe 4h --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --sma-fast 20 --sma-slow 50 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-30a78dfb1d2767ec23e9439ad9d15b59 --strategy momentum --timeframe 1d --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --momentum-lookback 24 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-30a78dfb1d2767ec23e9439ad9d15b59 --strategy breakout --timeframe 15m --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --breakout-lookback 20 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-30a78dfb1d2767ec23e9439ad9d15b59 --strategy mean_reversion --timeframe 1h --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --mean-reversion-lookback 30 --mean-reversion-entry-z -2 --mean-reversion-exit-z 0 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-6128f04036bd63e5480e52ca49e0db99 --strategy sma_cross --timeframe 4h --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --sma-fast 40 --sma-slow 100 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-6128f04036bd63e5480e52ca49e0db99 --strategy momentum --timeframe 1d --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --momentum-lookback 48 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-6128f04036bd63e5480e52ca49e0db99 --strategy breakout --timeframe 15m --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --breakout-lookback 50 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-6128f04036bd63e5480e52ca49e0db99 --strategy mean_reversion --timeframe 1h --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --mean-reversion-lookback 45 --mean-reversion-entry-z -2 --mean-reversion-exit-z 0 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-bc7c887114286bbf53145ad084891167 --strategy sma_cross --timeframe 4h --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --sma-fast 20 --sma-slow 50 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-bc7c887114286bbf53145ad084891167 --strategy momentum --timeframe 1d --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --momentum-lookback 24 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-bc7c887114286bbf53145ad084891167 --strategy breakout --timeframe 15m --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --breakout-lookback 20 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-bc7c887114286bbf53145ad084891167 --strategy mean_reversion --timeframe 1h --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --mean-reversion-lookback 30 --mean-reversion-entry-z -2 --mean-reversion-exit-z 0 --output artifacts/research/initial/20261029
uv run quantlab backtest --snapshot snapshot-11194982446605bc8ee412db4f6724e4 --strategy momentum --timeframe 1d --cash-usdt 1000 --fee-bps 10 --slippage-bps 5 --momentum-lookback 24 --output artifacts/research/initial/20261029
~~~

## Como continuar manualmente

1. Na raiz do projeto, execute uv sync e uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1.
2. Em Dados, confira o plano e baixe períodos fechados explicitamente. Para julho-setembro/2026 já existem ZIPs F1; execute a auditoria F2 de cada um antes de selecionar as partições. Os arquivos diários de outubro precisam ser auditados dia a dia.
3. Crie snapshots com partições 1m QA PASS explícitas e sem sobrepor fontes mensais/diárias. Não use partições quarantined.
4. Em Backtests, escolha o snapshot e regra, confirme timeframe, parâmetros, capital, taxa, slippage e atraso. Execute e revise o HTML e o JSON antes de comparar.
5. Em Pesquisa, crie uma nova versão e registre períodos, folds, valores de parâmetros permitidos, critério primário e o holdout futuro antes de executar trials. Mantenha a versão aberta durante desenvolvimento/validação; congele somente uma configuração que passe os critérios definidos antes dos resultados.
6. Use o período de teste apenas para uma regra congelada, compare com B&H e custos estressados, e abra o holdout final uma única vez por versão congelada. Depois de abrir, não reutilize a mesma versão para retuning.

O relatório técnico anexado está em [Plataforma de Trading Algorítmico com IA e Backtesting Avançado — pesquisa técnica e plano de implementação](<Plataforma de Trading Algorítmico com IA e Backtesting Avançado_ pesquisa técnica e plano de implementação>).

