# delegate-agy — Plano de correção pós-revisão adversarial

Status: pronto para implementação por outra IA
Data da revisão: 2026-08-28
Repositório: /home/matheusdm/.hermes/plugins/delegate-agy
Baseline: 23aa692a9707143e5b92edc3c5c3639af1ff604b
Plano normativo original: IMPLEMENTATION_PLAN.md

## 1. Objetivo

Corrigir os defeitos encontrados na revisão técnica independente da implementação
0.2.0 do plugin delegate-agy, preservando o desenho original:

~~~text
Hermes plugin → AgyManager → subprocesso oficial "agy" → Antigravity
~~~

O resultado deve manter:

- execução direta do binário oficial "agy";
- nenhum proxy HTTP, bridge, extração de credenciais ou provider customizado;
- workers assíncronos e paralelos com limite configurável;
- isolamento por sessão e ownership fail-closed;
- process groups POSIX supervisionados de ponta a ponta;
- records atômicos e recovery seguro entre múltiplos runtimes;
- retenção limitada, raw output por tentativa e continuidade/steering;
- testes default que não consumam quota real.

Este documento é um plano de implementação. A IA implementadora deve verificar o
código atual diretamente e não assumir que o diagnóstico abaixo é suficiente sem
reproduzir cada caso.

## 2. Findings que obrigatoriamente devem ser corrigidos

### H1 — Process group sobrevive à conclusão natural ou a exceções pós-spawn

Áreas atuais: tools.py, aproximadamente _execute_once() nas linhas 856–895,
shutdown() nas linhas 1299–1354.

Comportamento reproduzido:

1. um fake "agy" faz fork();
2. o descendente permanece no mesmo PGID, fecha stdout/stderr e dorme;
3. o líder imprime um JSON SUCCESS e termina;
4. o manager marca o worker como succeeded e remove _processes;
5. o descendente e o PGID continuam vivos, inclusive após shutdown().

Também foi reproduzido com communicate() levantando OSError: o finally atual
apenas remove o processo do mapa e fecha pipes, deixando o grupo vivo.

Impacto: processos podem continuar executando ou modificando arquivos depois de
um resultado terminal e depois do unload do plugin.

Contrato violado: ownership do grupo inteiro, cleanup pós-Popen, término bounded
em qualquer saída e nenhum grupo sobrevivente após stop/timeout/unload.

### H2 — shutdown() libera o lease com futures ainda vivos

Área atual: tools.py, aproximadamente linhas 1342–1354.

Comportamento reproduzido com um _execute_once() bloqueado além de
SHUTDOWN_WAIT_SECONDS:

~~~text
shutdown() retorna
future.done() == False
_runtime_lease_fd == None
record.status == running
runtime B carrega o record como interrupted
runtime A depois grava cancelled
~~~

Impacto: runtimes diferentes podem reivindicar, interromper e sobrescrever o
mesmo record enquanto o runtime original ainda pode alterá-lo.

Contrato violado: lease exclusivo durante toda a vida de um manager, executor
encerrado antes da liberação do lease e recovery que nunca interrompe trabalho
ainda supervisionado por outro runtime.

### M1 — IDs persistidos não são validados antes de formar paths ou globs

Áreas atuais: _load_existing_records(), _read_record(), _record_path(),
_runtime_lease_is_locked(), _terminal_bundles() e _delete_terminal_bundle().

Um JSON em runs/agy_seed.json com worker_id: "../../escaped-record" foi aceito e
fez o recovery escrever plugin-data/escaped-record.json, fora de runs/.

Além de traversal, um worker_id divergente do nome do arquivo pode fazer recovery
ou retenção operar sobre o bundle de outro worker.

Contrato violado: reload seguro por worker ID, ausência de traversal e nenhuma
corrupção cruzada de records/bundles.

### M2 — Falha durante o construtor do manager vaza FD/lease

Áreas atuais: aquisição do lease e _load_existing_records() em __init__().

Se a gravação do estado interrupted falhar durante a recuperação, o construtor
levanta a exceção depois de abrir o lock, sem liberar o descritor. O probe
observou um novo FD apontando para <runtime-id>.lock após a falha.

Impacto: tentativas repetidas de discovery/recovery acumulam FDs e locks órfãos.

### M3 — Falha na persistência inicial deixa record queued sem future

Área atual: _new_record().

O record é colocado em _records antes da primeira gravação. Se essa gravação
falhar, handle() retorna erro, mas o record permanece queued, sem future. O
shutdown() atual pode gravá-lo como queued com cancel_requested=true e sem
finished_at.

Contrato violado: falhas de spawn nunca deixam records duráveis eternamente
queued; records cancelados devem ser terminais.

### M4 — Parser aceita log JSON status-only como resultado final

Área atual: _parse_payload().

Entrada reproduzida:

~~~json
{"status":"SUCCESS","response":"actual-result","conversation_id":"conv-1"}
{"status":"SUCCESS","event":"cleanup-log"}
~~~

O segundo objeto é selecionado como payload final e o worker é considerado
sucesso com resposta vazia e sem conversation ID.

Contrato violado: aceitar JSON compacto/multiline com logs antes/depois, mas
rejeitar objetos de log que apenas parecem envelopes de resultado.

## 3. Restrições obrigatórias

1. Leia integralmente IMPLEMENTATION_PLAN.md antes de editar.
2. Leia integralmente /home/matheusdm/.hermes/hermes-agent/AGENTS.md antes de
   qualquer inspeção do Hermes Core.
3. Não modifique nenhum arquivo do Hermes Core.
4. Preserve alterações existentes, inclusive IMPLEMENTATION_PLAN.md não rastreado.
5. Não execute tests/integration_smoke.py.
6. Não invoque um agy real nem consuma quota; use somente executáveis fake,
   subprocessos locais e mocks.
7. Não faça git commit nem git push durante a implementação.
8. Não substitua testes comportamentais por testes que leem texto-fonte.
9. Não remova nem enfraqueça contratos já atendidos: ownership por session_id,
   argv exato, start_new_session=True, flock, atomic replace, limites de memória
   e output e registro Hermes.
10. Toda nova espera de processo deve ser bounded e nunca ocorrer segurando o
    RLock do manager.

## 4. Preflight e estado a preservar

Antes de editar, registre:

~~~bash
cd /home/matheusdm/.hermes/plugins/delegate-agy
git rev-parse HEAD
git status --short --untracked-files=all
git diff --stat
~~~

Preserve os arquivos já modificados:

- README.md
- __init__.py
- plugin.yaml
- tools.py
- tests/test_unit_mock.py
- tests/test_process_groups.py
- tests/test_batch_and_ownership.py
- tests/test_persistence_concurrency.py
- tests/test_shutdown_lifecycle.py
- tests/test_hermes_contract.py
- IMPLEMENTATION_PLAN.md

O único novo arquivo previsto por este plano é CORRECTION_PLAN.md, salvo se
forem necessários testes adicionais claramente justificados.

## 5. Desenho recomendado da correção

### 5.1 Estado de ownership de um subprocesso

Não redescubra o PGID durante cleanup. Depois de Popen():

1. capture imediatamente pgid = os.getpgid(process.pid);
2. valide pgid > 0, pgid == process.pid e pgid != os.getpgrp();
3. só então publique (process, pgid) em _processes e persista PID/PGID;
4. mantenha essa informação local até o cleanup terminar;
5. nunca substitua o PGID capturado por uma nova chamada a getpgid().

Se a captura/validação falhar, não envie sinal a um grupo não validado. Termine
o líder de forma segura, tente reap/drain bounded e registre failed; documente
que descendants não podem ser garantidos quando o PGID nunca foi capturado.

### 5.2 Cleanup obrigatório em _execute_once()

Reestruture o método para que toda execução após Popen() esteja sob um
try/finally que não dependa de persistência bem-sucedida.

O cleanup deve:

- observar o PGID capturado, mesmo que o líder já tenha saído;
- em caso de timeout, cancelamento, exceção de communicate(), falha de
  persistência ou sucesso natural, verificar os.killpg(pgid, 0);
- se o grupo ainda existir e pertencer ao worker, enviar SIGTERM, aguardar com
  time.monotonic() até deadline bounded e enviar SIGKILL se necessário;
- reapear o filho direto com process.wait(timeout=...) curto;
- drenar pipes com limite curto; em falha de drain, fechar handles e registrar
  diagnóstico bounded;
- só remover _processes[worker_id] depois dessa sequência;
- ser idempotente para chamadas concorrentes de stop e shutdown.

Após communicate() retornar normalmente, o líder pode estar morto mas o grupo
ainda existir. Isso não deve ser tratado como sucesso limpo: faça cleanup do
grupo capturado antes de liberar ownership. O payload já lido pode continuar
sendo interpretado, mas nenhum descendant deve permanecer.

Não chame communicate() sem timeout em nenhum caminho. Não faça wait() ou drain
ilimitado no finally.

### 5.3 Transições terminais

Use um único helper lock-required, como _transition(), para atualizar status,
erro e timestamps. Garanta que:

- timed_out permaneça timed_out, sem conversão posterior para failed;
- cancelamento concorrente com sucesso natural resulte deterministicamente em
  cancelled ou succeeded, mas nunca em estado não terminal;
- qualquer exceção não observada do supervisor resulte em failed com finished_at;
- falha de persistência terminal seja best-effort sem ressuscitar queued;
- nenhuma espera de processo ocorra enquanto _lock está segurado.

### 5.4 Shutdown e lease: ordem e deadline

Implemente esta sequência sob as invariantes do plano:

1. adquirir _lock e marcar _closed exatamente uma vez;
2. cancelar futures queued individualmente;
3. para cada future efetivamente cancelado antes de iniciar, transicionar o
   record para cancelled com finished_at;
4. para records não terminais sem future e sem processo, também fazer transição
   terminal (cancelled);
5. capturar snapshot de processos e PGIDs;
6. liberar _lock;
7. terminar grupos fora do lock;
8. aguardar futures com uma deadline monotônica global, nunca um timeout novo
   independente para cada future;
9. chamar executor.shutdown(cancel_futures=True);
10. liberar o runtime lease somente quando não houver future capaz de tocar
    records/processos.

Ponto crítico: shutdown(wait=False) não pode ser seguido imediatamente por
_release_runtime_lease() enquanto existirem futures vivos. Se a deadline global
expirar:

- não libere o lease prematuramente;
- mantenha o manager fechado para novas operações;
- use um reaper bounded/daemon ou mecanismo equivalente que observe os futures
  restantes e libere o lease somente depois que todos terminarem;
- torne essa liberação idempotente;
- não permita que novo runtime marque records do runtime antigo enquanto esse
  reaper ainda pode executar código do worker.

O reaper não deve manter o RLock durante waits. Documente claramente o estado
closed-but-lease-held e teste-o.

shutdown() repetido deve ser inofensivo e não criar múltiplos reapers.

### 5.5 Exception safety do construtor

Depois de abrir o lease, trate a construção como uma transação de recursos:

- se criação de diretórios, executor, recovery ou retenção falhar, feche o FD
  do lease e faça cleanup de qualquer executor criado;
- não deixe um lock sem referência Python;
- preserve a exceção original;
- não use shutdown() se os campos necessários ainda não foram inicializados,
  a menos que o método seja explicitamente seguro para inicialização parcial.

Adicione teste que injeta erro na gravação de um record ativo durante startup e
verifica que FD/lock não ficou aberto.

### 5.6 Validação de records, worker IDs e runtime IDs

Centralize validação antes de qualquer operação de filesystem:

- worker_id deve satisfazer _WORKER_ID_RE;
- o stem do arquivo carregado deve ser exatamente o worker_id persistido;
- runtime_id deve ser formato estrito gerado pelo manager, por exemplo 32 hex
  chars, sem /, .. ou separadores;
- owner_id deve ser normalizado como string não vazia;
- _record_path() e o path do lease devem rejeitar entradas inválidas e, se
  apropriado, confirmar que o caminho resolvido permanece sob o diretório-base;
- _terminal_bundles() e _delete_terminal_bundle() só podem atuar sobre IDs
  previamente validados e cujo record corresponde ao nome do arquivo;
- record inválido/inconsistente deve ser ignorado ou quarentenado com log
  bounded, nunca reescrito usando path derivado do conteúdo não validado.

Garanta compatibilidade com JSON legado: campos novos continuam opcionais, mas
um record legado válido ainda precisa ter worker_id seguro.

### 5.7 Falha na persistência inicial do spawn

A operação deve ser atomicamente uma destas alternativas:

- record queued publicado e future registrado; ou
- nenhuma publicação queued, com erro estruturado retornado; ou
- record publicado e imediatamente transicionado para failed com timestamp.

Nunca deixe _records conter queued sem future depois que handle() retorna erro.
Se uma gravação falhar depois de os.replace(), trate o arquivo já publicado:
tente publicar failed ou remova-o com segurança; não o abandone como queued.

O shutdown deve terminalizar qualquer record residual não terminal sem
future/processo.

### 5.8 Parser JSON resiliente, mas não permissivo demais

Mantenha JSONDecoder.raw_decode() para objetos compactos/multilineares com logs
antes/depois. A seleção deve:

- rejeitar arrays como payload final;
- ignorar logs sem envelope de resultado;
- não considerar {"status":"SUCCESS","event":"cleanup"} um resultado só por
  ter status reconhecido;
- aceitar SUCCESS/SUCCEEDED/DONE/COMPLETED com pelo menos um campo de resultado
  (response, conversation_id ou equivalente do contrato);
- aceitar ERROR/FAILED com error estruturado, preservando prioridade sobre
  stderr/stdout;
- preservar o último payload de resultado realmente válido quando há vários;
- manter campos desconhecidos sem falhar;
- manter returncode != 0 sempre como falha;
- manter usage somente quando for mapping/dict;
- rejeitar bool, NaN e Infinity nas métricas numéricas suportadas.

Se o output oficial demonstrar combinação adicional válida, codifique-a em regra
explícita e teste; não volte ao critério genérico “qualquer status”.

## 6. Alterações de testes obrigatórias

Os testes devem ser comportamentais, usar somente tempfile, unittest.mock,
executáveis fake e subprocessos locais. Não leia tools.py ou outro código como
texto para validar implementação.

### Process lifecycle: tests/test_process_groups.py

1. Fake com líder que imprime SUCCESS, fecha/encerra, e descendente no mesmo PGID
   que fecha stdout/stderr e permanece vivo. Após wait, assert status terminal e
   grupo inexistente; após shutdown, assert novamente.
2. Wrapper de subprocesso real cujo communicate() levanta OSError; assert que o
   grupo é terminado, _processes fica vazio somente após cleanup e não sobra
   PID/PGID.
3. Verificar que getpgid() só é chamado na captura pós-spawn, nunca durante
   stop, timeout, sucesso ou shutdown.
4. Manter SIGTERM, SIGKILL, líder morto com descendant, pipe herdado, timeout e
   falha de persistência.

### Shutdown: tests/test_shutdown_lifecycle.py

1. Future que permanece vivo além de deadline reduzida. Assert que shutdown()
   não libera o lease enquanto o future pode tocar o record.
2. Tentar iniciar manager B no mesmo HERMES_HOME enquanto A está fechado mas
   ainda tem future vivo; B não pode marcar o record de A como interrupted.
3. Liberar o future e assert que o lease é finalmente liberado exatamente uma
   vez.
4. Manter cancelamento queued, spawn/shutdown concorrente, unload idempotente,
   callback CAS e exceção não observada.

### Persistência: tests/test_persistence_concurrency.py

1. Record com worker_id contendo .., / ou divergente do filename: não pode criar
   arquivo fora de runs/, sobrescrever outro worker ou entrar nos mapas.
2. Falha injetada durante recovery: nenhum FD de lease permanece aberto.
3. Falha injetada na primeira gravação de spawn: nenhum queued sem future e
   nenhum queued durável após shutdown.
4. Manter dois runtimes, writes atômicos, crash recovery, legado e permissões.

### Parser: tests/test_unit_mock.py

1. JSON de sucesso válido seguido de log JSON status-only: selecionar o sucesso
   real.
2. Log JSON com event, level, message e status reconhecido: ignorar.
3. Manter clean JSON, compact/multiline, logs antes/depois, malformed, arrays,
   returncode não zero, ERROR, prioridade de payload["error"], usage dict,
   bool/NaN/Infinity e campos futuros.

## 7. Ordem de implementação recomendada

### Fase A — Preflight e regressões

- registrar commit/status;
- ler os documentos normativos e AGENTS.md;
- adicionar primeiro os testes que reproduzem H1–H2 e M1–M4;
- confirmar que os novos testes falham pela razão pretendida, sem executar o
  smoke live.

### Fase B — Cleanup e process ownership

- refatorar _execute_once() e helpers de termination/drain;
- implementar cleanup no sucesso natural e em exceções genéricas;
- garantir remoção tardia de _processes;
- executar process groups e shutdown.

### Fase C — Shutdown e runtime lease

- corrigir deadline global, futures e cancelamento queued;
- implementar retenção do lease até todos os futures cessarem;
- tornar shutdown idempotente;
- tornar __init__ exception-safe;
- executar testes multiprocessos, incluindo repetição.

### Fase D — Persistência segura

- centralizar validação de IDs e correspondência filename/record;
- proteger paths/globs e recovery;
- corrigir falha de persistência inicial;
- verificar atomicidade, permissões, fsync e cleanup de temporários.

### Fase E — Parser e compatibilidade

- corrigir seleção de candidatos JSON;
- preservar contratos de interpretação existentes;
- executar unitários e parser adversarial.

### Fase F — Auditoria final

- revisar diff completo, incluindo novos testes e este arquivo;
- confirmar que nenhuma edição ocorreu no Hermes Core;
- repetir suíte completa três vezes;
- repetir persistência pelo menos 20 vezes;
- rodar diff --check e status;
- não criar commit nem executar push.

## 8. Comandos autorizados de verificação

Use executáveis fake e mocks somente. A suíte completa mínima é:

~~~bash
cd /home/matheusdm/.hermes/plugins/delegate-agy
PYTHONDONTWRITEBYTECODE=1 pytest \
  tests/test_unit_mock.py \
  tests/test_process_groups.py \
  tests/test_batch_and_ownership.py \
  tests/test_persistence_concurrency.py \
  tests/test_shutdown_lifecycle.py \
  tests/test_hermes_contract.py \
  -v -p no:cacheprovider
~~~

Se um arquivo novo de teste for criado, inclua-o na linha equivalente e liste a
alteração no relatório. Execute a suíte completa três vezes consecutivas após
implementar.

Para persistência, execute pelo menos 20 vezes:

~~~bash
for run_index in $(seq 1 20); do
  PYTHONDONTWRITEBYTECODE=1 pytest \
    tests/test_persistence_concurrency.py \
    -q -p no:cacheprovider || exit 1
done
~~~

Verificações finais:

~~~bash
git diff --check
git status --short --untracked-files=all
~~~

Não executar:

~~~bash
python tests/integration_smoke.py
~~~

## 9. Definition of Done

Só considerar concluída a implementação quando todos os itens forem verdadeiros:

- fake com líder encerrado e descendant sobrevivente não deixa PGID vivo;
- exceções pós-spawn não deixam processo, grupo, pipe ou FD aberto;
- stop, timeout, conclusão natural e unload têm cleanup bounded e terminal;
- nenhum wait()/communicate() ilimitado foi introduzido;
- shutdown retorna bounded, mas não libera lease enquanto futures ainda podem
  tocar records;
- novo runtime não interrompe runtime antigo ainda vivo ou ainda em reaper;
- shutdown repetido é idempotente;
- falhas de construtor e persistência inicial não vazam lease nem deixam queued
  órfão;
- IDs de records e runtimes são validados antes de path/glob;
- records legados válidos continuam carregáveis;
- temporários são únicos, atômicos, fsyncados, privados e removidos;
- parser aceita formatos válidos e rejeita logs result-shaped enganosos;
- contratos anteriormente atendidos continuam passando;
- suíte completa passa três vezes consecutivas;
- persistência passa pelo menos 20 repetições;
- git diff --check passa;
- nenhum arquivo do Core foi alterado;
- nenhum teste live foi executado e nenhuma quota foi consumida;
- nenhum commit ou push foi feito.

## 10. Relatório exigido da IA implementadora

Ao finalizar, informe:

1. resumo das correções por finding;
2. arquivos alterados e novos;
3. invariantes de process group, lease e persistência implementados;
4. comandos exatos e resultados de todas as execuções;
5. confirmação de que não rodou integration_smoke.py e não consumiu quota;
6. confirmação de que Hermes Core não foi modificado;
7. resultado de git diff --check;
8. resultado de git status --short --untracked-files=all;
9. riscos residuais ou limitações de plataforma;
10. confirmação de que não houve commit nem push.
