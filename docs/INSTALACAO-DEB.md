# Pacote Debian de teste

## Artefato gerado

`dist/files2gdrive_0.1.0-alpha3_amd64.deb`

Alvo: Ubuntu 24.04, arquitetura amd64, Python do sistema 3.12. Não é um pacote arm64 nem uma versão validada para outras versões do Ubuntu.

A versão é alpha. Os testes automatizados e a execução do pacote extraído são complementados por instalação/reinstalação/remoção em raiz dpkg descartável. Ainda é necessário validar a instalação limpa com sessão gráfica e as políticas do domínio Google Workspace utilizado.

## Instalar

Fechar o Files2GDrive antes de instalar, atualizar ou remover. No diretório do arquivo:

```bash
sudo apt install ./files2gdrive_0.1.0-alpha3_amd64.deb
```

O APT instala as dependências nativas necessárias. As dependências Python estão incluídas no pacote; não é necessário instalar pip ou montar um ambiente virtual na máquina destinatária.

Abrir **Files2GDrive** no menu de aplicativos ou executar:

```bash
/usr/bin/files2gdrive
```

A primeira execução prepara um perfil novo em modo Google Drive, sem pastas monitoradas nem autorização Google. A identificação OAuth Desktop do aplicativo está integrada; clicar em **Conectar ao Google** e autorizar a própria conta. Não é necessário criar projeto ou fornecer JSON por destinatário.

**O acesso depende da configuração do projeto OAuth do distribuidor.** Se estiver no modo de teste, somente contas adicionadas como testadoras conseguem autorizar o aplicativo. Para distribuição geral, o responsável pelo projeto precisa preparar sua publicação e atender aos requisitos do Google. Contas Workspace também estão sujeitas às políticas da organização.

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
  --output-dir dist --version '0.1.0~alpha3'
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
- O pacote não foi instalado no banco dpkg do Ubuntu do usuário. Essa validação isolada não substitui teste numa VM limpa com sessão gráfica.

O alpha3 inclui o seletor de pastas com exceções por subdiretório, indicador de autenticação, preparação de diretórios em lote e até quatro uploads simultâneos. Arquivos de até 5 MiB usam upload multipart; maiores usam upload retomável. O esquema atual do banco é 4; não execute o alpha1 sobre um perfil já migrado.

A aplicação usa a licença MIT, cujo texto acompanha o pacote em `/usr/share/doc/files2gdrive/copyright`. As dependências mantêm suas próprias licenças, incluídas no diretório privado do aplicativo.

Na release GitHub, o nome do instalador usa `0.1.0-alpha3`; a versão Debian interna permanece `0.1.0~alpha3`. O script de build local gera o nome com `~`.
