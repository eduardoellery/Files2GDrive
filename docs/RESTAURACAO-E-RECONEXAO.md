# Restauração e reconexão

Restauração disponível na interface web e pela CLI; a reconexão após reinstalar é feita pela CLI. Estes procedimentos usam o mesmo cliente OAuth de produção/desenvolvimento que criou os backups: o escopo `drive.file` não permite descobrir arquivos arbitrários pertencentes a outro aplicativo.

## Descobrir backups

Com o motor parado e o estado configurado/autenticado:

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado backups
.venv/bin/python -m files2gdrive --state-dir /caminho/estado backup-files UUID_COMPUTADOR --root-id UUID_PASTA
```

O catálogo lista arquivos ativos e retidos, sem os que já estão na lixeira. Nomes novos são guardados nos metadados; backups antigos sem esses nomes aparecem identificados por UUID. Backups sem arquivos ainda não aparecem no catálogo.

## Restaurar arquivos ou pastas

Escolher destino **novo**, fora das pastas monitoradas e do estado privado. Sua pasta pai deve existir.

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado restore \
  UUID_COMPUTADOR UUID_PASTA /caminho/novo-destino
```

Opções:

- `--active-only`: somente arquivos ativos, sem Excluídos.
- `--file-id UUID_ARQUIVO`: selecionar um arquivo; repetir para selecionar vários.
- `--path .config`: selecionar um arquivo ou uma subpasta e seus descendentes; repetir para selecionar vários. Caminhos são relativos à raiz do backup, sem barra inicial ou final.

Se ambas as seleções (`--file-id` e `--path`) forem usadas, elas se combinam por interseção. Uma chamada restaura uma única raiz de um único computador. Se duas identidades históricas tiverem o mesmo caminho, selecionar uma delas por ID para evitar ambiguidade.

Nenhuma pasta restaurada entra automaticamente no monitoramento. Arquivos no Drive não são modificados pela restauração.

### Proteções implementadas

- Validar versão dos metadados, UUIDs, caminho relativo, permissões comuns, data, tamanho e checksum.
- Rejeitar caminhos absolutos, `..`, NUL, nomes excedendo limites Linux e conflitos entre arquivos e diretórios.
- Não seguir links simbólicos nos componentes do destino.
- Baixar para temporário privado, validar conteúdo e publicar atomicamente sem substituir arquivos existentes.
- Aplicar permissões comuns e data de modificação após conferir o conteúdo.
- Em falha, remover o temporário incompleto; arquivos já restaurados permanecem disponíveis para inspeção. Uma nova tentativa deve usar outro destino novo.
- Revalidar metadados remotos antes e depois do download; alterações exigem atualizar o catálogo.

Datas suportadas pelo formato inicial de restauração: 0 até 2**63−1 nanossegundos desde Unix epoch, aproximadamente 1970–2262. Proprietário/grupo, ACLs, atributos estendidos, bits especiais, diretórios vazios e permissões de diretórios não são restaurados nesta etapa. Diretórios criados usam modo 0700.

## Reconectar após reinstalar

**Desligar o motor da instalação anterior antes de assumir sua identidade.** Dois motores com o mesmo identificador não devem operar o mesmo backup simultaneamente.

Usar diretório de estado novo, sem pastas/arquivos previamente cadastrados e sem filtros globais configurados. Configurar o mesmo cliente OAuth, autenticar a mesma conta Google e consultar `backups`.

Antes de reconectar, restaurar o conteúdo desejado para as pastas locais, se necessário. Pastas locais ausentes/inacessíveis não podem ser mapeadas.

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado-novo reconnect-prepare \
  UUID_COMPUTADOR \
  --map UUID_PASTA=/caminho/pasta-local
```

Repetir `--map` para cada raiz desejada. As demais raízes remotas são listadas como não gerenciadas e não são alteradas.

A prévia retorna `review_id` e contagens:

- `unchanged`: conteúdo e metadados locais coincidem.
- `upload`: arquivo local difere e deverá atualizar o remoto.
- `new_local`: arquivo local sem entrada no backup selecionado.
- `start_retention`: arquivo ativo remoto ausente localmente; iniciará retenção após retomar.
- `cancel_retention`: arquivo retido presente localmente; voltará ao backup ativo.
- `retained_missing`: arquivo retido ainda ausente; mantém a data original.
- `trash_expired`: subconjunto retido, ausente e já vencido; poderá ir à lixeira ao retomar.

A conferência lê os conteúdos locais para comparar checksums. Em acervos grandes isso pode demorar; a confirmação repete a verificação para detectar mudanças.

Enquanto houver uma reconexão pendente, o motor não executa backup/limpeza e não permite adicionar raízes, alterar filtros ou retomar. É possível atualizar a conferência ou cancelá-la sem modificar o Drive:

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado-novo reconnect-review
.venv/bin/python -m files2gdrive --state-dir /caminho/estado-novo reconnect-cancel
```

Após conferir, confirmar usando o digest exibido:

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado-novo reconnect-confirm \
  --review-id DIGEST_DA_CONFERENCIA
```

Se os arquivos locais/remotos mudaram, a confirmação é recusada; executar `reconnect-review` e conferir novamente.

A confirmação importa identidades e prazos, mas **mantém o backup pausado** e não executa a fila. Reconfigurar filtros desejados — filtros antigos não estão disponíveis nos metadados remotos atuais — e então retomar explicitamente:

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado-novo resume
.venv/bin/python -m files2gdrive --state-dir /caminho/estado-novo run
```

O namespace do cofre permanece independente da identidade do computador adotada, preservando a autenticação da instalação nova. Não são importados tokens de outros computadores.

A reconexão é recusada quando existem duas identidades retidas/ativas para o mesmo caminho na mesma raiz, pois o schema local atual só aceita uma. O catálogo e a restauração seletiva continuam disponíveis; esse caso precisa de ampliação de schema antes de reconectar diretamente.

## Validação desta etapa

- Testes automatizados de caminhos maliciosos, links, conflitos, checksum, downloads interrompidos, metadados, prévia, alterações após conferência e importação pausada.
- Teste real somente com os arquivos gerados na etapa anterior: configuração oculta, arquivo vazio e arquivo maior que um bloco. Conteúdo, modo e mtime_ns conferidos após restauração.
- Simulação de reinstalação com banco novo, catálogo real e importação pausada; nenhum motor retomado e nenhuma alteração remota nesse teste.

Referência oficial para os downloads: [Google Drive — download files](https://developers.google.com/workspace/drive/api/guides/manage-downloads).
