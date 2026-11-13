# Instalador Debian — release pública 0.1.1

## Artefato gerado

`dist/files2gdrive_0.1.1_amd64.deb`

Alvo: Ubuntu 24.04, arquitetura amd64, Python do sistema 3.12. Não é um pacote arm64 nem uma versão validada para outras versões do Ubuntu.

A release pública atual é `0.1.1`, sem sufixo de pré-lançamento. Ela corrige o bootstrap e o isolamento da sessão local; veja o [histórico de versões](../CHANGELOG.md). O mantenedor confirmou o fluxo de desinstalação, limpeza do estado de teste, nova autorização e reinstalação com backup em outro Ubuntu na `0.1.0`. Testes automatizados, execução do pacote extraído e instalação/reinstalação/remoção em raiz dpkg descartável complementam essa validação. Políticas específicas de domínios Google Workspace continuam dependentes de validação no ambiente alvo.

## Instalar

Fechar o Files2GDrive antes de instalar, atualizar ou remover. No diretório do arquivo:

```bash
sudo apt install ./files2gdrive_0.1.1_amd64.deb
```

O APT instala as dependências nativas necessárias. As dependências Python estão incluídas no pacote; não é necessário instalar pip ou montar um ambiente virtual na máquina destinatária.

Abrir **Files2GDrive** no menu de aplicativos ou executar:

```bash
/usr/bin/files2gdrive
```

A primeira execução prepara um perfil novo em modo Google Drive, sem pastas monitoradas nem autorização Google. A identificação OAuth Desktop do aplicativo está integrada; clicar em **Conectar ao Google** e autorizar a própria conta. Não é necessário criar projeto ou fornecer JSON por destinatário.

**O projeto OAuth da distribuição oficial está em produção** desde a atualização informada pelo mantenedor em 2 de novembro de 2026; não é mais necessário cadastrar cada conta como testadora. O domínio teve a propriedade verificada no Google Search Console e o site público usa HTTPS obrigatório com certificado válido. Contas Workspace continuam sujeitas às políticas da organização. Quem construir uma distribuição com cliente OAuth próprio precisa configurar e publicar esse projeto separadamente; em Testing, continuam valendo suas restrições de contas testadoras. A versão pública atual do aplicativo é `0.1.1`.

Estado por usuário: `$XDG_STATE_HOME/files2gdrive` quando absoluto; caso contrário, `~/.local/state/files2gdrive`. Tokens continuam no Secret Service/cofre do Ubuntu.

## Convivência com a versão de desenvolvimento

O pacote não altera perfis ou atalhos pessoais em scripts de instalação. Um atalho de desenvolvimento em `~/.local/share/applications/files2gdrive.desktop` tem prioridade sobre o atalho global instalado.

Para mudar para o pacote, primeiro sair do aplicativo de desenvolvimento. Seu comando `remove-session`, com o estado de desenvolvimento correto, remove somente os atalhos gerenciados desse perfil:

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado-desenvolvimento remove-session
```

Depois abrir `/usr/bin/files2gdrive`. O perfil novo fará seu próprio login; o estado e os backups do desenvolvimento não são apagados ou migrados automaticamente.

A opção `--state-dir` continua disponível para perfis explicitamente selecionados. Não apontar dois motores ao mesmo backup/identidade ao mesmo tempo.

## Inicialização da sessão

O pacote instala lançador global e entrada XDG autostart. Na próxima sessão, o aplicativo inicia sem abrir navegador. A opção **Sair** não provoca reinício imediato: a reabertura acontece ao abrir o app ou entrar em outra sessão.

Administradores podem ajustar a entrada de autostart em `/etc/xdg/autostart/files2gdrive.desktop`, tratada como conffile. Um usuário pode desativá-la criando sua própria entrada com o mesmo nome e `Hidden=true`, conforme XDG Autostart. O comando `remove-session` gerencia as entradas pessoais criadas explicitamente; não remove arquivos globais do pacote.

## Atualização e remoção

Atualizar instalando um novo `.deb` via APT, após sair do aplicativo. Não há atualização automática nesta versão.

```bash
sudo apt remove files2gdrive
```

Para remover também a configuração global de autostart do pacote:

```bash
sudo apt purge files2gdrive
```

Remoção/purge não apagam perfil pessoal, tokens no cofre nem arquivos no Drive. O pacote não inicia processos como root nem executa scripts de instalação/remoção em diretórios pessoais.

## Recomeçar com um perfil novo para teste

O mantenedor validou o fluxo de desinstalação, remoção do estado de teste, nova autorização e reinstalação com backup em outro Ubuntu. A remoção normal preserva os dados; a limpeza para repetir a configuração inicial é uma ação separada e opcional.

1. No Ubuntu de testes, saia do aplicativo e aguarde o motor terminar as operações em andamento.
2. **Antes de desinstalar ou mover o perfil**, remova a credencial dessa instalação do cofre:

   ```bash
   /usr/bin/files2gdrive logout
   ```

3. Remova o pacote e sua configuração global:

   ```bash
   sudo apt purge files2gdrive
   ```

4. Para iniciar sem a configuração anterior, renomeie o perfil. No caminho padrão:

   ```bash
   mv ~/.local/state/files2gdrive \
      ~/.local/state/files2gdrive-teste-anterior
   ```

   Use um nome de destino ainda inexistente. Se configurou `XDG_STATE_HOME` absoluto ou `--state-dir`, use o caminho correspondente. Não mova nem apague as pastas originais escolhidas para backup. O perfil arquivado não deve ser reutilizado neste teste de instalação limpa.

5. Gerencie manualmente os backups do teste no Google Drive. Envie à lixeira somente a pasta desse computador/teste; não exclua backups de outras instalações. Não é necessário esvaziar a lixeira para repetir a instalação.
6. Se quiser repetir também a concessão de acesso no Google, remova o vínculo nas [conexões da Conta Google](https://myaccount.google.com/connections). Isso é opcional: `logout` já remove a credencial local. A revogação no Google pode invalidar a autorização do mesmo aplicativo em **outras instalações que usam a mesma conta**, não apenas no Ubuntu de testes. Revogar não apaga arquivos do Drive.
7. Reinstale o `.deb`, abra o aplicativo e confirme o pedido de login e a seleção inicialmente vazia. Configure um pequeno backup e teste envio, alteração, exclusão e reinicialização.

A atualização do mesmo projeto OAuth de Testing para produção é uma configuração do Google; não exige substituir o JSON OAuth integrado nem limpar perfis de quem apenas atualiza o aplicativo. O pacote `0.1.1` mantém o mesmo cliente da distribuição oficial. Não refaça a limpeza ao atualizar uma instalação que deve continuar seu backup existente.

## Verificar integridade

No diretório dos artefatos:

```bash
sha256sum --ignore-missing -c SHA256SUMS
```

Há um manifesto SHA256 por arquivo ao lado do pacote. O único cliente OAuth incluído é a identificação do aplicativo Desktop. Nenhum token/autorização de usuário, banco, log, arquivo monitorado, `.runtime*` ou `.venv` foi incluído.

## Construção

O builder recebe dependências previamente reunidas e um cliente Desktop válido:

```bash
.venv/bin/python scripts/build_deb.py \
  --vendor-dir /caminho/vendor \
  --oauth-client /caminho/cliente-desktop.json \
  --output-dir dist --version '0.1.1'
```

Versões e hashes das 44 dependências estão em `packaging/requirements-release.lock`. Para preparar o vendor com pip em um ambiente Python de construção:

```bash
python -m pip install --target /caminho/vendor --only-binary=:all: \
  --require-hashes -r packaging/requirements-release.lock
```

A construção usa uma whitelist de módulos/assets e rejeita tokens no JSON OAuth, links simbólicos e arquivos de credencial no vendor. Não copia o ambiente de desenvolvimento. Os subprocessos instalados reexecutam o lançador do pacote, mantendo os caminhos de app/vendor em vez de depender de `PYTHONPATH` ou do checkout.

## Validações realizadas

- Inspeção do controle/conteúdo, dono root:root, permissões e checksums; lançadores passaram em `desktop-file-validate`.
- Payload formado por whitelist e dependências vendorizadas; bibliotecas nativas devem ser verificadas no ambiente alvo.
- Wrapper extraído executado com `/usr/bin/python3`, sem `.venv`; perfil novo em Drive/autenticação pendente e zero raízes.
- Descoberta estática do SDK Google, subprocesso do motor via lançador e comandos pausa/retomada/saída validados, sem login ou upload.
- Simulação APT aceitou o pacote e as dependências no ambiente atual.
- Instalação, reinstalação, remoção e purge passaram em raiz/banco dpkg temporários. Dependências foram forçadas apenas nesse banco vazio; dependências reais foram verificadas separadamente. Um arquivo representando estado pessoal sobreviveu intacto.
- As validações automatizadas usam raiz dpkg isolada. Separadamente, o mantenedor confirmou a instalação e o fluxo de recomeço com autenticação e backup em outro Ubuntu com sessão gráfica.

A versão `0.1.1` inclui as funcionalidades da `0.1.0` e as correções de segurança descritas no changelog: o arquivo de bootstrap usa token por POST e a API exige bearer por sessão. O esquema atual do banco é 4; não execute o alpha1 sobre um perfil já migrado.

A aplicação usa a licença MIT, cujo texto acompanha o pacote em `/usr/share/doc/files2gdrive/copyright`. As dependências mantêm suas próprias licenças, incluídas no diretório privado do aplicativo.

A versão Debian interna e a do instalador público são `0.1.1`, com arquivo `files2gdrive_0.1.1_amd64.deb` e tag Git `v0.1.1`. A instalação atualiza a `0.1.0` pelo APT e preserva estado e dados pessoais. A pré-release `0.1.0~alpha3` permanece disponível no histórico, com seus artefatos originais.
