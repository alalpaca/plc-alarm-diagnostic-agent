DSM 项目中文流程总结
整个系统的目标是：

以 SharePoint 作为文件源，通过 Power Automate 自动同步文件到 Linux Web Server，由用户在前端确认哪些文件需要参与更新或删除，再由 Web Server 向 HPC 发起 RAG 构建请求，并在构建完成后把最新 RAG 拉回 Web Server。

1. SharePoint 作为文件源头
SharePoint 中包含多个 folder，每个 folder 下有多个文件。

用户平时在 SharePoint 上进行文件维护，例如：

新增文件
修改文件
删除文件
只要 SharePoint 中的 folder 发生文件变更，就会触发后续自动化流程。

2. Power Automate 触发整个 Folder 的文件传输
当 SharePoint folder 检测到文件更新时，Power Automate 会被触发，执行整个 folder 的自动化 File Transfer，把相关文件同步到 Linux Web Server。

这里的特点不是只处理单个文件，而是基于 整个 folder 的同步 来做更新。

3. Linux Web Server 接收文件并统一存放
Linux Web Server 是文件同步后的落地点。

从 SharePoint 传过来的文件会存放到服务器上的统一目录（图中的 Total Folder）。

这个 Web Server 既负责保存同步过来的文件，也负责后续文件筛选和状态管理。

4. Python Script 基于 Data Status Form 进行文件筛选与状态更新
Web Server 上运行一个 Python Script，它的核心作用是：

结合同步过来的整个 folder 文件内容
对照 Data Status Form 这张表
筛选出真正需要参与后续 RAG 构建的文件
同时更新文件状态信息
这里的 Data Status Form 本质上是一张状态表，用来记录每个文件的处理信息，例如：

Title
Date
Modified time
Deleted（T/F）
Finished（T/F）
Status type（new / modified / delete）
所以这一步可以理解为：

自动化同步先把文件全部传过来，Python Script 再结合状态表做一次“筛洗”，识别哪些文件是新增、修改或删除，并更新 Data Status Form。

5. 前端由用户确认哪些文件需要进入新一轮 RAG 构建
前端提供一个 UI，用户可以看到 Data Status Form 中识别出来的文件变化情况。

在这一步，用户需要人工确认：

哪些新增文件需要进入新的 RAG 构建
哪些修改过的文件需要重新进入构建
哪些文件需要从 RAG 中删除
也就是说，系统不会仅仅因为文件发生变化就立刻重建 RAG，

而是先经过用户在前端的确认，再决定实际要处理的文件范围。

用户确认后的结果会回写到 Data Status Form。

6. 用户确认后，Web Server 向 HPC 发起 RAG 构建请求
在用户通过前端确认新增/修改/删除文件，并更新了 Data Status Form 之后，

Linux Web Server 会根据这张表中确认后的状态，向 HPC Server 发起 RAG request，请求执行新的 RAG 构建。

这里可以理解为：

触发方：Linux Web Server
执行方：HPC Server 上的 RAG Pipeline
7. HPC 上执行 RAG Pipeline，并由 SQL Status Manager Database 管理构建状态
HPC Server 负责真正执行 RAG 构建流程。

其内部包括：

RAG Pipeline
SQL Status Manager Database
RAG（latest）
其中 SQL Status Manager Database 用来记录和管理当前构建任务的状态。

例如构建任务可能会经历：

已提交
处理中
成功
失败
8. Web Server 轮询 HPC 的 SQL Status Manager Database 获取构建状态
在发起 RAG 构建请求之后，Linux Web Server 不会被动等待，而是通过 轮询 的方式去查询 HPC 上 SQL Status Manager Database 中的任务状态。

也就是说：

Web Server 会定期检查这次 RAG 构建是否完成
如果状态还未完成，则继续等待
如果状态变成成功，则进入下一步
9. 当 HPC 构建成功后，Web Server 把最新 RAG 拉回本地
当 SQL Status Manager Database 中显示该次 RAG 构建最终成功时，

Web Server 会从 HPC 上把新生成的 latest RAG 拉回到 Web Server。

这样 Web Server 上就持有了当前最新版本的 RAG 结果。

10. 最终形成完整闭环
因此整个闭环是：

SharePoint 文件更新
Power Automate 自动传输整个 folder 到 Linux Web Server
Python Script 结合 Data Status Form 做文件筛选和状态更新
用户在前端确认哪些文件需要进入 RAG 更新或删除
前端更新 Data Status Form
Web Server 根据确认后的状态向 HPC 发起 RAG 构建请求
HPC 执行 RAG Pipeline，并在 SQL Status Manager Database 中维护任务状态
Web Server 轮询任务状态
构建成功后，将最新 RAG 从 HPC 拉回 Web Server
一版更简洁的流程描述
如果你要对别人快速解释，可以说：

系统以 SharePoint 作为文件源，一旦 folder 中文件发生变更，Power Automate 会自动把整个 folder 同步到 Linux Web Server。Web Server 上的 Python Script 会结合 Data Status Form 识别新增、修改和删除的文件，并更新状态表。随后用户在前端确认哪些文件需要参与新的 RAG 构建或从 RAG 中删除。确认完成后，Web Server 会向 HPC 发起 RAG 构建请求，并通过轮询 HPC 上的 SQL Status Manager Database 获取任务状态。待构建成功后，Web Server 再把最新的 RAG 拉回本地，完成一次完整的更新流程。

我帮你整理成“模块职责”版
1. SharePoint
文件源头
存放原始 folder 和 file
用户在这里进行新增、修改、删除
2. Power Automate
监听 SharePoint folder 的变化
自动执行整个 folder 的文件传输
3. Linux Web Server
接收并存储同步过来的文件
维护 Total Folder
运行 Python Script
管理 Data Status Form
接收前端确认结果
向 HPC 发起 RAG 构建请求
轮询构建状态
拉取最新 RAG
4. Data Status Form
文件状态管理表
记录文件元信息和状态
支撑 Python Script 的筛选逻辑
支撑前端确认和后续 RAG 构建触发
5. Frontend
给用户展示文件变更状态
让用户确认哪些文件需要进入构建或删除
将确认结果更新回 Data Status Form
6. HPC Server
执行 RAG Pipeline
生成新的 RAG
用 SQL Status Manager Database 管理构建状态
如果写成“File 流程”的正式表达，可以这么写
File Processing Workflow
Step 1: File change detection
Files are maintained in SharePoint folders. Any file creation, modification, or deletion in a SharePoint folder triggers the automation flow.

Step 2: Folder-level file transfer
Power Automate transfers the entire updated folder from SharePoint to the Linux Web Server.

Step 3: File filtering and status update
A Python script on the Linux Web Server processes the transferred folder, filters the relevant files based on the Data Status Form, and updates the file status table accordingly.

Step 4: User confirmation in frontend
The frontend displays the detected file changes to the user. The user confirms which files should be included in the new RAG build and which files should be removed.

Step 5: Trigger RAG build
After the user confirmation updates the Data Status Form, the Linux Web Server sends a request to the HPC Server to start the RAG build process.

Step 6: Polling build status
The Linux Web Server periodically polls the SQL Status Manager Database on the HPC Server to monitor the RAG build status.

Step 7: Retrieve latest RAG
Once the build status becomes successful, the Linux Web Server pulls the newly generated latest RAG from the HPC Server.