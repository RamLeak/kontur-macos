/** Bounded outbox for one intentionally typed new-chat draft, before it has an ID. */
type StorageLike=Pick<Storage,"getItem"|"setItem">;
type DraftState={text:string;conversationId:string|null;dirty:boolean;pending:boolean;uncertain:boolean;error:string|null};
type Dependencies={create:()=>Promise<string>;save:(id:string,text:string)=>Promise<boolean>;ready:(id:string)=>void};
const memoryDrafts=new Map<string,string>();
export class NewConversationDraft{
 private state:DraftState={text:"",conversationId:null,dirty:false,pending:false,uncertain:false,error:null};
 private operation:Promise<void>|null=null;
 private listeners=new Set<()=>void>();
 private key:string;
 constructor(owner:string,projectId:string|null,private storage:StorageLike|null,private dependencies:Dependencies){
  this.key=`kontur:new-conversation-draft:v1:${encodeURIComponent(owner)}:${encodeURIComponent(projectId??"no-project")}`;
  try{const raw=memoryDrafts.get(this.key)??storage?.getItem(this.key);if(raw&&raw.length<100_000){const saved=JSON.parse(raw);
   if(saved.dirty===true&&typeof saved.text==="string"&&saved.text.length<=16000)this.state={text:saved.text,
    conversationId:typeof saved.conversationId==="string"?saved.conversationId:null,dirty:saved.dirty===true,
    pending:false,uncertain:saved.uncertain===true||(saved.pending===true&&!saved.conversationId),error:null};
  }}catch{}
 }
 snapshot=()=>this.state;
 subscribe=(listener:()=>void)=>{this.listeners.add(listener);return()=>{this.listeners.delete(listener);};};
 private update(patch:Partial<DraftState>){this.state={...this.state,...patch};
  const raw=JSON.stringify(this.state);if(memoryDrafts.has(this.key)||memoryDrafts.size<32)memoryDrafts.set(this.key,raw);
  try{this.storage?.setItem(this.key,raw);}catch{}
  this.listeners.forEach(listener=>listener());
 }
 edit(text:string){this.update({text,dirty:true,error:this.state.uncertain?this.state.error:null});}
 acknowledge(text:string){if(this.state.text===text)this.update({dirty:false,error:null});}
 localRecoverable(){if(!this.state.dirty)return true;try{return JSON.parse(this.storage?.getItem(this.key)??"null")?.text===this.state.text;}catch{return false;}}
 allowExplicitRetry(){this.update({uncertain:false,error:null});}
 flush():Promise<void>{
  if(this.operation)return this.operation;
  if(!this.state.dirty)return Promise.resolve();
  if(this.state.uncertain&&!this.state.conversationId)return Promise.reject(new Error("Создание чата не подтверждено. Проверьте историю или повторите явно."));
  if(!this.state.text.trim()&&!this.state.conversationId){this.update({dirty:false});return Promise.resolve();}
  const save=async()=>{
   this.update({pending:true,error:null});
   try{
    let id=this.state.conversationId;
    if(!id){id=await this.dependencies.create();this.update({conversationId:id});}
    this.dependencies.ready(id);
    for(let attempt=0;attempt<3&&this.state.dirty;attempt++){
     const text=this.state.text;const confirmed=await this.dependencies.save(id,text);
     if(!confirmed)throw new Error("Черновик сообщения ещё не подтверждён сервером.");
     this.update({dirty:this.state.text!==text});
    }
   }catch(reason){this.update({dirty:true,uncertain:!this.state.conversationId,
    error:!this.state.conversationId?"Не удалось подтвердить создание чата. Проверьте историю перед повтором.":"Не удалось сохранить черновик. Проверьте подключение и повторите попытку."});throw reason;}
   finally{this.update({pending:false});}
  };
  this.operation=save().finally(()=>{this.operation=null;});return this.operation;
 }
}
