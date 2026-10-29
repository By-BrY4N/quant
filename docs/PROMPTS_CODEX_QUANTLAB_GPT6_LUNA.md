# QUANTLAB — PROMPTS DE IMPLEMENTAÇÃO PARA CODEX (GPT-6 LUNA HIGH / EXTRA HIGH)

**Versão:** 2.0 | **Data:** 16/10/2026 | **Uso:** prompts sequenciais, prontos para copiar/colar.  
**Especificação de produto:** `PLANO_FINAL_QUANTLAB_IMPLEMENTACAO_2026.md`.  
**Objetivo:** software **entregável, usável localmente e verificável manualmente**, não maratona de testes nem produção antecipada de documentação.

> **Importante:** selecionar **GPT-6 Luna / High** na interface real do Codex, quando disponível; usar **Extra High** para dados temporais, contabilidade, validação científica e bugs difíceis. Escrever “use GPT-6” num prompt não muda tecnicamente o modelo configurado. Se a ferramenta não disponibilizar Luna ou subagentes, trabalhar com o melhor modelo/effort realmente disponível e **não inventar recursos**.

## Modo de uso

1. Coloque **os dois arquivos** Markdown na pasta `docs/` do repositório. A v1 serve como referência histórica opcional, não como instrução conflitante.
2. **Na primeira sessão**, envie `PROMPT 00` e, após o agente compreender as regras, `PROMPT F0`. Também é válido colar ambos no mesmo pedido se o Codex conseguir ler o plano.
3. **Uma fase por sessão/tarefa**, sem tentar F0→F8 autonomamente. Iniciar F1 somente após testar manualmente o entregável de F0.
4. No encerramento, exigir **comandos executáveis**, arquivos alterados, prints textuais/artefatos verificados, limitações e passos manuais. Não pedir novo documento de planejamento no lugar do código.
5. Se algo quebrar, usar o **PROMPT FIX**, limitado ao defeito, em vez de mandar “refaz tudo”.

### Política de modelos/agentes

| Trabalho | Preferência | Delegação |
|---|---|---|
| F0, F1, F4, integração e docs | Luna **High** | Um agente principal. |
| F2 parser/tempo e F3 ledger/custos | Luna **Extra High** | Um implementador e, se suportado, um revisor **read-only**. |
| F5 walk-forward/holdout, F6 ML/leakage | Luna **Extra High** | Auditor separado apenas para revisão de causalidade, sem editar arquivos centrais. |
| F7 paper replay | Luna **High**, subir para Extra High se houver bug de estado/dinheiro | Um implementador + revisão pontual. |
| F8 expansões | High, Extra High somente na parte de alta complexidade | Subagentes apenas para módulos totalmente independentes. |

Se houver execução multiagente real, limitar preferencialmente a **2 agentes ativos de código/revisão** por vez; ninguém deve editar simultaneamente `ledger.py`, `clock.py`, `catalog.py`, `settings.py` ou interfaces compartilhadas. Um revisor pode sugerir patch, mas **quem integra é o agente principal**. Se o ambiente não oferecer isso, fazer as mesmas verificações no agente único.

---

# PROMPT 00 — ORQUESTRADOR, CONTRATO E REGRA DE NÃO DESPERDIÇAR EXECUÇÃO

```text
Você é o agente principal de implementação de um projeto Python local chamado QuantLab.

CONFIGURAÇÃO DE EXECUÇÃO (quando suportada pela plataforma):
- Modelo preferido: GPT-6 Luna.
- Esforço: High; Extra High somente nas tarefas críticas de temporalidade, dinheiro, leakage, concorrência e revisões difíceis.
- Agentes auxiliares: somente se a ferramenta permitir. Usar no máximo um revisor read-only ou colaborador independente; não assumir ferramentas de orquestração inexistentes.

FONTE DE VERDADE:
Leia integralmente docs/PLANO_FINAL_QUANTLAB_IMPLEMENTACAO_2026.md, especialmente os contratos de dados, execução, artefatos, segurança e os critérios de pronto. Se o arquivo estiver em outro local, encontre pelo nome no repositório. A versão anterior do plano é histórico, não fonte dominante em conflitos.

PRIORIDADE INEGOCIÁVEL:
ENTREGAR CÓDIGO FUNCIONAL E UM FLUXO MANUAL UTILIZÁVEL. Não faça uma reescrita do plano, centenas de issues, nem instale frameworks não exigidos. Não construa corretora/live trading.

REGRAS GLOBAIS:
1. Trabalhe apenas na fase indicada na mensagem subsequente. Não implemente fases futuras preventivamente.
2. Ao começar, inspecione o repositório, reutilize arquivos existentes e faça no máximo um plano de execução curto com 3-7 itens. Depois codifique.
3. Python 3.12 preferencial; uv + lockfile; tecnologias do plano são orientação, não licença para empilhar dependências. Mantenha o ambiente Windows/WSL utilizável.
4. Preserve contratos financeiros: nenhum futuro em sinais, nenhum fill antes do instante elegível, nenhuma duplicação de fee, nenhum saldo negativo, raw imutável e nenhum broker live.
5. Faça um único caminho end-to-end funcionar antes de generalizar. Sem interfaces/arquivos vazios para cumprir aparência de arquitetura.
6. TESTES: até 1 rodada de verificações automatizadas focalizadas nos riscos alterados, até 1 smoke end-to-end e até 1 lint focalizado por fase. Caso falhe, corrija e reexecute SOMENTE a verificação problemática. Não rode pytest global, fuzz, carga, benchmark, laços de autorrevisão ou múltiplos testes de larga escala por hábito. Exceção: nunca ignore BUG CONHECIDO de dinheiro, tempo, checksum ou segurança: reduza escopo e corrija a causa.
7. Não baixe anos de dados em testes. Downloads remotos só quando indispensáveis e pequenos; fixtures sintéticas determinísticas para CI. Se rede falhar, construa caminho import/offline e descreva o que não pôde verificar.
8. Sempre faça distinção visível entre dados SINTÉTICOS e dados reais; resultados são pesquisa, não promessa de lucratividade.
9. Não adicione credenciais, chave privada, endpoint de ordem real, nem funções de compra/venda na exchange.
10. Não mude metodologia silenciosamente. Decisões novas/limitações reais em docs/DECISIONS.md e docs/CHANGELOG_IMPL.md. Não declare pronto o que não foi executado.
11. Priorize usabilidade do Streamlit e artefatos HTML/JSON, mas UI não deve duplicar lógica de negócio.
12. Se ficar bloqueado por API/dependência, escolha alternativa mínima e segura, preserve escopo e registre bloqueio. Não faça cinco novas bibliotecas para contornar um import.

ENTREGA AO FINAL DE CADA FASE:
- Resumo curto do que realmente foi implementado.
- Lista de arquivos alterados.
- 2-5 comandos exatos, da raiz do repositório, que eu posso executar.
- Roteiro manual em 3-8 passos com comportamento esperado.
- Qual verificação automatizada/smoke foi executada, resultado e LIMITAÇÕES não verificadas.
- O que fica de fora para a próxima fase.

Crie/atualize AGENTS.md na raiz com uma versão CURTA e estável destas regras, sem duplicar os dois planos completos. Respeite AGENTS.md nas sessões posteriores.
Aguarde apenas a definição da fase se ela ainda não tiver sido fornecida; quando F0 for indicado, comece a programar sem pedir confirmação repetitiva.
```

---

# PROMPT F0 — FUNDAÇÃO, ENGINE MÍNIMA E DEMO OFFLINE

**Modelo recomendado:** Luna High. **Critério:** primeira sessão precisa terminar com algo que rode.

```text
Implemente SOMENTE a fase F0 do plano final, em código executável. Não faça apenas scaffolding.

ENTREGAS:
A. Projeto uv instalável em Python 3.12, CLI `quantlab` com `--help` e `demo`.
B. Estrutura mínima de domínio para Candle, Signal/TargetExposure, SimulatedFill, PortfolioState, RunArtifacts.
C. Fixture sintética PÚBLICA, claramente rotulada SINTÉTICA em CLI/relatório, com candles suficientes para demo visual e fixture numérica isolada de compra/venda.
D. Um laço determinístico de spot long-only para CASH e BUY_HOLD com ledger em USDT/base_qty e fee em USDT. Não implemente short, margem nem futuros.
E. Diretório artifacts/<run_id> com run.json, fills, equity, metrics e report.html (CSV pode substituir Parquet só na demo, se for mais simples, documentando).
F. Uma página Streamlit muito simples ou página HTML navegável que mostre run, equity, fills e origem SINTÉTICA. Não há problema em deixar a UI aprimorada para F4.
G. README com instalação e comandos concretos no Windows PowerShell e WSL; AGENTS.md, .gitignore e docs/CHANGELOG_IMPL.md.

REGRA ESPECÍFICA DE LEDGER:
Fixture com capital inicial 1000.00 USDT, compra de 1 unidade a 100.00, venda 1 unidade a 110.00, taxa 0,10% em USDT por lado, sem slippage: caixa final 1009.79 e taxas 0.21. Garanta esse caso com teste focado. Não confundir essa fixture com a política de comprar 100% da carteira.

NÃO:
- Não implemente download Binance.
- Não implemente ML, Nautilus, Freqtrade, Otimização ou paper forward.
- Não baixe dados reais e não invente resultado de performance.
- Não execute testes em loop; 1 teste focado para ledger e 1 execução demo.

ACEITE MANUAL:
1. `uv sync` funciona.
2. `uv run quantlab demo` cria arquivo e mostra RUN_ID.
3. HTML/Streamlit abre e sinaliza “dados sintéticos”.
4. O saldo da fixture de contabilidade bate em 1009.79 e não há posição remanescente.
5. Reabrir o relatório não exige rede.

Ao terminar, mostre exatamente os comandos e resultados que você verificou. Se uma etapa não funciona, não declare F0 completo.
```

---

# PROMPT F1 — BINANCE: DOWNLOAD/IMPORTAÇÃO VERIFICADA, NÃO UM SCRAPER GIGANTE

**Modelo recomendado:** Luna High; apoio opcional de agente *somente leitura* conferindo docs oficiais.

```text
Leia o estado atual do repositório e o plano final. Implemente SOMENTE F1, sem reescrever F0.

OBJETIVO:
Conseguir adquirir com segurança um ZIP mensal oficial de BTCUSDT spot 1m e criar manifest verificável; também permitir importação offline do mesmo ZIP.

TAREFAS CONCRETAS:
- CLI `quantlab data plan` para 1 ou poucos meses, sem baixar, mostrando nome, URLs oficiais, n arquivos e aviso de espaço desconhecido quando aplicável.
- CLI `quantlab data fetch` para o par BTCUSDT spot com ZIP + `.zip.CHECKSUM` de https://data.binance.vision . Endpoint construído com allowlist estrita de símbolo/timeframe/ano/mês/host; verificar redirects.
- Streaming para `.part` e commit atômico. SHA-256 local conferido contra checksum publicado. ZIP CRC, entries e limite de tamanho antes de aceitar; proteção zip-slip/zip-bomb.
- Salvar original em data/raw/ por hash/identidade, sem sobrescrever. Registrar SQLite: source URL, datas, hash, byte size, ETag se houver, revisão/status. Idempotência em reexecução, registro de erro/404/429/retries limitados.
- CLI `quantlab data import --zip ... --checksum ...` passando pelo mesmo validador; se checksum não vier, classificar a integridade original como NÃO VERIFICADA, não PASS.
- Criar instruções de 1 mês exemplificado para 2024-01. Não baixar multi-ano automaticamente.

SEM CONTAMINAR ARQUITETURA:
Não parsear todo o lake nesta fase, não implementar futures, nem escrever API keys.

VERIFICAÇÃO ENXUTA:
1 teste focado para integridade, host/arquivo inseguro e idempotência com ZIP sintético local (pode ser parametrizado num único teste); 1 smoke de `plan/import` offline. Um fetch real pequeno é checagem manual se houver rede, não deve bloquear construção.

ACEITE MANUAL:
- Visualizar plano de um mês.
- Baixar mês (se rede) ou importar ZIP conhecido e checksum.
- Reimportar e ver “já verificado/pulado”, sem duplicação.
- Corromper uma cópia do ZIP e observar rejeição, sem alterar o objeto original.

Atualize README/CHANGELOG_IMPL. Entregue comandos exatos; avise honestamente o que não foi testado na rede.
```

---
Trabalhe somente na fase F2 do plano final. Não instale outra stack por conveniência e não refaça F1.

OBJETIVO:
Converter ZIPs Binance SPOT klines de 1 minuto para Parquet versionado e snapshot com qualidade explícita. Resultado precisa ser exibido por CLI, não apenas salvo no banco.

REGRAS DE DADOS OBRIGATÓRIAS:
1. Parser de 12 colunas nativas, verificando se header existe sem descartar a primeira linha de dados.
2. Timestamp: arquivos spot anteriores a 2025-01-01 em milissegundos; arquivos spot desde 2025-01-01 em microssegundos. Validar contra mês solicitado e unidade documentada; não adivinhar só por número de dígitos.
3. UTC; candle como [open_at,end_at), end = open + 1 minuto; source_close_time preservado. available_at = end + delay_assumed positivo, identificado como ASSUMED.
4. Campo open/high/low/close coerente, preço >0, volume >=0, trade_count>=0, sem duplicata conflitante; gap e porcentagem de cobertura.
5. Uma barra 1h só é PASS se composta de 60 candles de 1m PASS; 4h só se 240. Sem ffill nem candle incompleto disfarçado.
6. Quarentena de conflito/checksum/schema. Criar snapshot com lista exata de partições PASS, parser/schema version e hashes; nunca ler `latest` silenciosamente para executar pesquisa.
7. Consultar com DuckDB e gravar relatório de QA JSON/HTML; CLI data audit e data snapshot.

ANTES DE CODEX TESTAR:
Use fixtures pequenas de 2024 e 2025 com mesmo instante real representado em ms/us, mais um gap de 1m. 1 rodada focalizada de verificação dos três invariantes; 1 smoke pipeline import->normalize->audit->snapshot. NÃO baixar um ano, NÃO benchmark 10 milhões de linhas, NÃO criar battery de property-based tests.

ACEITE MANUAL:
- Mês 2024 resulta em ano 2024; arquivo 2025 resulta em 2025.
- Uma hora completa agrega exatamente 60 candles e seus OHLCs.
- Remover 1 minuto sinaliza janela incompleta, bloqueada para pesquisa.
- Dados brutos e hashes permanecem intocados; snapshot aponta aos IDs corretos.

Entregue comandos e uma tabela textual de exemplo de qualidade calculada pelo código, nunca inventada.

---

# PROMPT F3 — BACKTEST REAL, CAUSAL E CONTABILMENTE EXPLICÁVEL

**Modelo recomendado:** Luna Extra High. **Mais importante que passar muitos testes é não errar a ordem temporal nem o dinheiro.**

```text
Implemente a fase F3 com o snapshot real validado pela fase F2. Não substitua a engine por Freqtrade/Nautilus e não crie duas semânticas distintas de fills.

DEFINIÇÃO EXATA:
- Spot long-only e sem alavancagem, uma moeda quote USDT e uma posição base.
- Decisões em 1h/4h a partir de candles fechados e QA PASS; warm-up antes de operar.
- Candle 1h [14:00,15:00) fica disponível em 15:00:01 (delay assumido de 1s por padrão). Ordem permitida somente no PRIMEIRO candle 1m cuja ABERTURA é ESTRITAMENTE POSTERIOR a `available_at`, tipicamente 15:01. Nunca usar o open de 15:00.
- Fill market proxy no open desse minuto, com slippage adverso configurado em bps por lado e taxa quote separada. Não cobrar spread duas vezes. Não usar H/L/C da barra de execução para decidir/fixar ordem.
- Portfolio: cash>=0, base_qty>=0, no short, sem gastar fee além do disponível, equity=cash+base_qty*mark.
- Implementar cash, buy_hold, sma_cross e breakout, sem grid search. Sinal retorna target; executor decide qty/fee/fill. Sem limit/partial/margin.
- Gerar run.json, signal/fill/equity, metrics.json e relatório HTML; registrar política, snapshot e configuração exatos; comparador cash/buyhold mesma política.
- Exibir explicitamente lacunas, vetos, posições abertas, custos e nível IN_SAMPLE.

IMPORTANTE:
`available_at` histórico é hipótese de disponibilidade, não latência empiricamente medida. Os custos são hipóteses, não fee oficial Binance. A UI e o relatório precisam dizer isso.

VERIFICAÇÃO ENXUTA E CRÍTICA:
- 1 teste/fixture ledger 1000->1009.79 com taxa 0,10%.
- 1 teste focalizado para não executar antes de available_at e não modificar histórico ao acrescentar futuro.
- 1 smoke real de 1 mês ou fixture se remoto indisponível. Não rodar teste global em loop.

ACEITE MANUAL:
1. CLI backtest executa sma_cross (1h) e gera RUN_ID.
2. Compare fee=0 vs fee>0 e veja diferença apropriada.
3. Leia 3 linhas signal/fill, confirme relógio e fonte de preço.
4. Verifique à mão uma compra/venda: caixa, qty, fee, retorno.
5. Reproduza um run com os mesmos dados e verifique métricas equivalentes.

Ao final indique limitações OHLCV como aproximação de fill e não declare “rentabilidade garantida”.
```

---

# PROMPT F4 — PAINEL STREAMLIT DE VERDADE + RELEASE M1

**Modelo recomendado:** Luna High. Agente de UI pode atuar isolado se houver orquestração real e contratos estáveis.

```text
Implemente F4 sobre F0-F3 existentes. Objetivo: uma pessoa usar QuantLab sem editar Python, com dados reais ou demo, do início ao relatório.

TELAS OBRIGATÓRIAS:
1. Dados: status vazio/demo/real, fonte, cobertura, gaps, QA, snapshot, botões/controlos de importação e plano de fetch com confirmação antes de download.
2. Backtests: escolher snapshot/símbolo/timeframe, cash/buy_hold/SMA/breakout, parâmetros, capital, fee e slippage, rodar e abrir RUN_ID. Curva de equity, drawdown, benchmark e mensagem sobre custos hipotéticos.
3. Operações: tabela com signal_at/available_at/fill_at, preços, qty, taxa, pnl, veto/reason; filtros e export CSV. Selecionar trade e ver detalhes.
4. Runs anteriores: abrir relatório HTML/JSON e visualizar proveniência; se for demo, banner “DADOS SINTÉTICOS”.

UX:
- Sem métricas inventadas nem gráficos placeholders.
- Estados vazios com próxima ação clara; warnings são texto, não apenas cor.
- Erros do backend mostrados sem traceback gigante ao usuário, mas logs internos disponíveis.
- Não copiar cálculo de P&L ou estratégia para app.py. UI só orquestra funções de domínio.
- Streamlit responsivo; usar Plotly simples e eficiente, sem frontend React.

VERIFICAÇÃO:
1 smoke local da interface/carregamento de runs e uma checagem focalizada caso mude domínio. Evite Selenium/Playwright, screenshot diffs, 20 fixtures ou testes de estilo. Escreva checklist manual com 6-8 passos para navegação.

DEFINIÇÃO DE ACEITE M1:
Abrir sem dados; fazer demo; importar um mês Binance; auditar; rodar SMA/buyhold; examinar fills e taxas; baixar relatório; fechar e reabrir mantendo runs.

CONCLUSÃO:
Crie um breve RELEASE_M1.md com o que funciona de verdade, limitações e comandos exatos. Não apresente F5, ML ou paper forward como já construídos.
```

---
# PROMPT F5 — BACKFILL LIMITADO, COMPARAÇÃO E VALIDAÇÃO CIENTÍFICA M2

**Modelo recomendado:** Luna Extra High no contrato de holdout; High para a UI complementar.

```text
Agora implemente SOMENTE a fase F5 do PLANO FINAL, após inspecionar o código existente e respeitar o M1 estável.

OBJETIVO:
Transformar o laboratório em um pesquisador quantitativo com vários runs comparáveis e walk-forward honesto, sem inflar a complexidade estatística.

BACKLOG DESTA FASE:
- Ampliar origem spot Binance para BTCUSDT/ETHUSDT e múltiplos meses mediante comando explícito; planejamento de n ZIPs/limite de transferência; lidar com fonte monthly/daily, overlap e revisões de forma rastreável.
- Não baixar todo 2020–2026 no teste. Um mês ou pequeno intervalo é suficiente no desenvolvimento; usuário decide backfill completo.
- Experimento versionado: família, parâmetros, nº de tentativas, snapshot, config hash, métricas primárias, custos e split.
- Comparador UI de 2–4 runs, recusando ou sinalizando se diferem instrumentos, períodos, snapshots ou execução.
- Walk-forward temporal com treino e teste subsequente, sem shuffle; manter warm-up causal. Holdout final INACESSÍVEL por tuning. Para modelos futuros, interface deve aceitar horizon/purge, mas NÃO programar DSR/PBO/White agora.
- Adicionar métricas por períodos e custo 2x/3x, drawdown, turnover e exposição. Permitir NO-GO / INCONCLUSIVO / CANDIDATO_A_PAPER sob critérios registrados ANTES da avaliação.
- Se a cobertura de BTC/ETH for diferente, não ocultar; registrar exclusões e não afirmar que os períodos são iguais.
- UI Pesquisa/Comparação mostrando o que foi treinado/selecionado e holdout selado. Uma consulta final de holdout após congelamento deve registrar evento e proibir edições sob mesma versão.

CUIDADOS:
O split ilustrativo 2020–2023 / 2024–2025 / 2026-01–09 só vale se cobertura de cada ativo for suficiente. Não inventar teste final se ainda não há dados do período. Ganhar menos buyhold não significa necessariamente perder a hipótese de redução de risco; exigir métrica primária pré-registrada.

VERIFICAÇÃO:
1 teste focalizado de partição temporal/holdout e 1 smoke com poucas barras/meses; executar lint uma vez se necessário. Não criar suíte estatística enciclopédica, não rodar 200 variantes para popular gráfico.

ACEITE MANUAL:
- Comparar dois runs compatíveis e ver custos/riscos.
- Tentar comparar períodos diferentes e ler aviso explícito.
- Ver holdout selado e registrar um trial.
- Executar fold curto; salvar métricas/run manifest; bloquear tuning após abrir avaliação final.

Atualize docs e apresente o roteiro de reprodução.
```

---

# PROMPT F6 — PRIMEIRO ML QUE NÃO ESPREITA O FUTURO

**Modelo recomendado:** Luna Extra High. Revisor temporal opcional read-only.

```text
Implemente SOMENTE F6, sem desmontar as estratégias existentes nem instalar deep learning/RL.

META:
Experimento supervisionado simples, temporalmente correto, comparado no mesmo motor/custos contra modelo nulo e regras determinísticas.

O QUE IMPLEMENTAR:
1. Escolher UMA tarefa inicial com explicação no código/README: classificação de retorno futuro acima de barreira de custos OU regressão de retorno futuro. Definir H, label_start/label_end e data em que label se torna observável.
2. Feature builder causal com pequeno conjunto: retornos defasados, rolling vol/range, distância SMA, volume relativo. Feature timestamp + available_at. Nunca usar `center=True`, normalização global ou ffill de dados futuros.
3. Split temporal com treino e validação, e gap/purging se labels de treino cruzarem o começo da validação; fit de scaler/imputer/modelo somente no treino de cada fold.
4. Modelo nulo + Ridge OU LogisticRegression, escolhendo a classe conforme alvo. Segundo modelo opcional (`HistGradientBoosting` ou LightGBM) apenas após primeiro circuito funcionar, nunca vários boosters simultaneamente.
5. Até 10 trials no primeiro experimento ML, todos rastreados; threshold calibrado só na validação. Se predição probabilística, Brier/log loss e curva de calibração; se regressão, MAE e correlação apropriada.
6. Converter previsão em TargetExposure ou abstenção por regra pré-definida e alimentar a MESMA engine spot de F3 com fee/slippage, não backtest alternativo conveniente.
7. Página de ML/relatório exibe intervalo treino/validação, pred at, label_end, número de exemplos e comparação modelo nulo/linear/baseline de estratégia. Se não houver ganho, exibir “SEM GANHO INCREMENTAL”.

PROIBIDO:
- Não baixar novas fontes/macro/news durante esta fase.
- Não usar holdout para ajustar thresholds, nem visualizar e repetir até ganhar.
- Não usar LLM para prever diretamente próximo candle.
- Não afirmar que 60% de acurácia implica lucro.

VERIFICAÇÃO:
Um teste direcionado com futura alteração de valores que não modifica previsões anteriores, um smoke pequeno do pipeline train->predict->backtest->report. Nada de 100 seeds, tuning massivo ou dezenas de rodadas de Pytest.

ACEITE MANUAL:
- Inspecionar uma previsão e provar que nenhuma feature/label futura foi usada no fit.
- Alterar final do dataset e confirmar que primeiro período não mudou.
- Visualizar nulo, linear e SMA sob mesmo universo/período/custo.
- Exportar predições e evidência do fold.

Documente os limites da evidência antes de concluir F6.
```

---

# PROMPT F7 — PAPER REPLAY OFFLINE COM ESTADO, VETO E REINÍCIO

**Modelo recomendado:** Luna High, subir para Extra High se erro de idempotência/ledger.

```text
Implemente F7 do PLANO FINAL, sem acesso a exchanges reais e sem afirmar que replay histórico é paper forward.

ALVO:
Acompanhar, PAUSAR, RETOMAR e INSPECIONAR uma estratégia congelada avançando no tempo histórico, com ledger idempotente.

OBRIGATÓRIO:
- Um replay controller percorre candles 1m na ordem temporal; view da estratégia só expõe informações disponíveis até o relógio atual.
- Estados STOPPED, READY, RUNNING, DEGRADED, HALTED com razões/transições persistidas. Dados incompletos ou divergência de ledger bloqueiam abertura de novas posições.
- O replay usa mesmas regras de preço/taxas/tempo de F3, ou documenta explicitamente diferença de política.
- RiskGate: veto por QA FAIL/UNKNOWN essencial, falta de barras, limite de exposição, caixa negativo, drawdown da simulação e duplicidade de event ID.
- Event journal/checkpoint persistente; reinício reexecuta eventos sem duplicar fills ou taxa. Estado HALTED exige reset/revisão explícita.
- UI com status, tempo simulado, posição/cash/equity, event log, botões pause/resume/stop/reset. Não usar `sleep` longos ou loops infinitos dentro do render do Streamlit; navegação e refresh controlados.
- Comparação replay vs backtest do mesmo snapshot, com diferenças listadas.
- Sem API keys privadas, sem socket de exchange e sem ação real de comprar/vender.

VERIFICAÇÃO FOCALIZADA:
Teste curto de duplicidade/restart e um smoke que pause/retome; não faça soak test de uma semana nem stress 10 mil usuários.

ACEITE MANUAL:
1. Iniciar replay e ler tempo atual.
2. Pausar, reiniciar app e retomar sem segundo fill.
3. Provocar gap sintético e observar HALTED + motivo, sem ordem nova.
4. Exportar journal de eventos e conciliar equity.

Escreva limitações no relatório como `PAPER_REPLAY`, jamais `PAPER_FORWARD`.
```

---

# PROMPT F8 — EXPANSÃO POR REQUISIÇÃO, NÃO CONSTRUIR TUDO

**Modelo recomendado:** High ou Extra High de acordo com o subprojeto escolhido.

```text
Estamos na fase F8. NÃO implemente todas as fontes, todos os mercados nem todas as engines.

Primeiro leia o subprojeto explicitamente escolhido nesta sessão (por exemplo PAPER_FORWARD, OKX_SPOT, PERP_FUNDING, L1_SPREAD, ALFRED_PIT ou NAUTILUS_VALIDATION), e a seção F8 do plano. Faça uma RFC CURTA (10–20 linhas) que explique:
- hipótese específica e benefício não atendido no M1–M3;
- fonte/dados e licença ou situação NÃO VERIFICADA;
- temporalidade/retificações;
- interfaces afetadas;
- aceite observável e o que será adiado.

Depois implemente SOMENTE esse subprojeto, isolando-o do core existente e com demonstração manual.

GATES ESPECÍFICOS:
- PAPER_FORWARD: precisa de fonte de dados efetivamente recente, candle fechado, relógio e heartbeat, risco fail-closed; sem endpoint de ordens reais. Não chamar daily archive de live.
- PERP_FUNDING: bloquear sem metadata de contrato, marcação, funding, margem e settlement. Jamais reutilizar ledger spot como perp alavancado.
- OKX_SPOT: símbolos/métricas identificam venue e instrument separadamente; não comparar fills sem equivalência de custos.
- L1/L2: confirmar sequência, snapshots e intervalos inválidos; touch de limit não é garantia de fill.
- ALFRED: release/revision/vintage devem ser elegíveis historicamente; observação econômica não é publicação.
- NAUTILUS: fazer PoC isolada com MESMA fixture, mapear diferenças de semântica; não substituir engine canônica sem equivalência demonstrada.

Um smoke integrado pequeno e testes focalizados no risco específico. Não refazer o projeto inteiro.

Finalize com um novo roteiro manual e o status real (DONE / PARTIAL / BLOCKED), sem inventar cobertura, preços ou lucro.
```

---

# PROMPT AUDIT — REVISÃO INDEPENDENTE, PONTUAL, SEM RESCREVER ARQUITETURA

**Modelo recomendado:** Luna Extra High. Ideal para agente secundário read-only.

```text
Atue como auditor READ-ONLY do QuantLab nesta fase. NÃO altere nenhum arquivo, não rode testes repetidos, não instale dependências, não refatore, não acrescente funcionalidades.

Examine apenas os arquivos alterados desde a última fase e os contratos da fase em docs/PLANO_FINAL_QUANTLAB_IMPLEMENTACAO_2026.md. Priorize rigorosamente:
1. Qualquer forma concreta de look-ahead (fechamento/available_at/fill no mesmo instante impossível, label leak, future rolling, join as-of incorreto).
2. Saldo, quantidade, fee, slippage duplicado, equity/P&L, ids duplicados.
3. Checksum ZIP, host allowlist, sobrescrita de raw, unidade ms/us, descarte de candle com gap.
4. Caminho real de broker/private API ou segredo, que é proibido.
5. UI afirmando o que o backend não suporta, dados de demo apresentados como reais.

Reporte NO MÁXIMO 8 achados relevantes, ordenados como BLOQUEANTE, IMPORTANTE e MELHORIA, cada um com arquivo/linha e menor correção recomendada. Se não houver evidência de problema, diga expressamente “nenhum bloqueante identificado nos arquivos analisados”, sem inventar defeitos hipotéticos.

Não faça revisão estilística de tudo nem proponha Kubernetes/ML avançado. Envie relatório ao agente principal.
```

---

# PROMPT FIX — CORREÇÃO CIRÚRGICA DE DEFEITO ENCONTRADO MANUALMENTE

**Modelo recomendado:** Luna High; Extra High para tempo/ledger/leakage.

```text
Corrija APENAS o problema abaixo no QuantLab existente, preservando a funcionalidade das fases já aprovadas.

DEFEITO OBSERVADO:
[COLE O ERRO OU DESCREVA PASSO A PASSO, ENTRADA, RESULTADO ATUAL E ESPERADO]

REGRAS:
- Leia os arquivos envolvidos; localize a causa antes de editar.
- Prefira o menor patch coerente, sem reescrever módulos inteiros nem introduzir uma biblioteca nova só por conveniência.
- Se for bug financeiro/temporal, acrescente UMA fixture/regressão minúscula que reproduza o erro e rode SOMENTE esse teste após corrigir.
- Para os demais bugs, uma validação manual/smoke reproduzindo o caso basta se não há risco sistêmico.
- Não rode a suíte completa 5 vezes e não faça refatoração oportunista.
- Atualize docs/CHANGELOG_IMPL.md e mostre arquivos/linhas relevantes, comando de reprodução e resultado observado.
- Se o erro depender de informação indisponível, documente o bloqueio em vez de inventar um fallback de dados ou P&L.
```

---

# PROMPT RESUME — RETOMAR APÓS INTERRUPÇÃO OU LIMITE DE CONTEXTO

**Modelo recomendado:** Luna High.

```text
Retome o trabalho no repositório QuantLab sem recomeçar a implementação.

1. Leia AGENTS.md, docs/PLANO_FINAL_QUANTLAB_IMPLEMENTACAO_2026.md, docs/CHANGELOG_IMPL.md e o estado do Git.
2. Identifique a última fase realmente ENTREGUE e a fase atual em andamento. Não confie cegamente em checkboxes; confira os arquivos, artefatos e os últimos comandos registrados.
3. Faça resumo de no máximo 10 linhas com o que funciona, o que está quebrado e uma sequência curta para fechar SOMENTE a fase atual.
4. Continue o código pendente; não revisite escolhas já estabilizadas nem implemente fase futura.
5. Execute apenas testes/smoke faltantes relevantes; não reexecute todos os anteriores por padrão.
6. Entregue roteiro manual e atualize o changelog.

Se o contexto anterior disser que algo foi testado mas não houver prova/artefato, classifique como NÃO VERIFICADO, não como sucesso.
```

---

# PROMPT RELEASE — FECHAR UMA FASE COM EVIDÊNCIA E PREPARAR HANDOFF

**Modelo recomendado:** Luna High.

```text
Feche SOMENTE a fase [F0/F1/F2/F3/F4/F5/F6/F7/F8-subprojeto] do QuantLab. NÃO acrescente novas features nem reestruture código.

Faça uma checagem de aceitação objetiva comparando o que existe com o contrato da fase no plano final.

Saída solicitada:
A. STATUS: DONE, PARTIAL ou BLOCKED, com justificativa verificável.
B. Comandos exatos e saídas reais já observadas; não inventar logs.
C. Lista concisa dos arquivos/módulos implementados.
D. Uma única lista de 3–8 passos manuais para o dono reproduzir a fase, incluindo o comportamento esperado.
E. Quais riscos críticos foram verificados e quais não foram; o que ainda impede a próxima fase.
F. Atualizar docs/CHANGELOG_IMPL.md e docs/DECISIONS.md se necessário.

Não rode a suíte de testes novamente se a fase já possui evidência direcionada atual, salvo correção concreta posterior que a invalide. A apresentação do release não substitui a implementação.
```

---

# PROMPT AGENTS.MD — CONTEÚDO CURTO PARA O ARQUIVO RAIZ

> O Prompt 00 instrui o agente a escrever `AGENTS.md`. Se ele criar uma versão burocrática ou enorme, substitua pelo texto abaixo.

```markdown
# AGENTS.md — QuantLab

## Objetivo
Entregar laboratório local de dados, backtesting e pesquisa quantitativa, utilizável e verificável manualmente. Plano: `docs/PLANO_FINAL_QUANTLAB_IMPLEMENTACAO_2026.md`. Prompts por fase: `docs/PROMPTS_CODEX_QUANTLAB_GPT6_LUNA.md`.

## Prioridades
1. Um caminho end-to-end que funcione antes de generalizar.
2. Tempo causal, raw íntegro, ledger reconciliável, nenhum envio de ordem real.
3. Reutilizar código existente; implementar apenas a fase solicitada.
4. Nunca apresentar dados sintéticos como mercado, hipóteses de execução como fills reais, ou backtest como retorno futuro.
5. CLI/Streamlit são interfaces do MESMO domínio.

## Verificação proporcional
Por fase: um teste automatizado focalizado de risco + um smoke end-to-end + lint pontual se houver código alterado. Corrigir falhas e reexecutar apenas o caso falho. Não executar suítes inteiras, load tests, fuzz ou tuning em loop. Nunca ignorar bug conhecido de dinheiro, tempo, checksum ou segurança.

## Agentes
Se houver subagentes de verdade, delegar somente revisão read-only/documentação ou módulos isolados. Agentes não editam os mesmos arquivos compartilhados simultaneamente. Preferir GPT-6 Luna High onde disponível; Extra High para bugs de contabilidade, temporalidade, vazamento e desenho estatístico. Não assumir que o prompt muda configuração do modelo.

## Entrega obrigatória
Ao concluir: estado DONE/PARTIAL/BLOCKED; arquivos; comandos executados e saída real; 3–8 passos manuais; limitações. Atualizar `docs/CHANGELOG_IMPL.md`. Proibidos live trading, credenciais privadas e refatorações de conveniência sem solicitação.
```

---

## Critérios gerais que os prompts devem sempre respeitar

**Não negociar:** sem envio de ordens reais, sem contas privadas, sem futures disfarçados, sem paper replay chamado forward.

**Não alucinar o resultado:** se não baixou arquivo, não escrever “checksum confirmado”; se não executou backtest, não preencher relatório de lucro fictício; se não rodou na máquina do usuário, registrar compatibilidade como “não verificada”.

**Não fazer teatro de engenharia:** repositório de 200 módulos vazios, 70 ADRs, 10 CI workflows e 500 testes não vale mais que uma tela navegável com resultados reproduzíveis. Mas também não suprimir os 4 invariantes cruciais (relógio, dinheiro, integridade dos dados e bloqueio de trading real).

**Parada consciente:** uma fase está concluída quando seu artefato visível funciona e seus riscos críticos foram confrontados. Depois pare, forneça comandos e deixe a validação manual determinar a próxima fase.
