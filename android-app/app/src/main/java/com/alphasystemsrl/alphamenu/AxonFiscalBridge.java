package com.alphasystemsrl.alphamenu;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.math.BigDecimal;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

/** DADO RT / Axon-Micrelec fiscal transport. A claimed sale is never sent twice. */
final class AxonFiscalBridge {
    private static final String BASE="https://menu.alphasystemsrl.it";
    private final SharedPreferences jobs;
    AxonFiscalBridge(Context context){jobs=context.getSharedPreferences("alpha_fiscal_jobs",Context.MODE_PRIVATE);}

    synchronized JSONObject dispatch(String path,JSONObject data)throws Exception{
        if("/fiscal/probe".equals(path)){idle(data.getJSONObject("config"),false);return new JSONObject().put("ok",true).put("message","DADO RT raggiungibile e libero. Nessun documento emesso.");}
        if("/fiscal/config/read".equals(path))return readProgramming(data.getJSONObject("config"),data.optJSONObject("batch"));
        if("/fiscal/config/write".equals(path))return writeProgramming(data.getJSONObject("config"),data.getJSONObject("programming"));
        if(!"/fiscal/emit".equals(path)&&!"/fiscal/recover".equals(path))throw new IllegalArgumentException("Operazione fiscale Android non valida.");
        String id=UUID.fromString(data.optString("id")).toString(),secret=data.optString("secret");
        if(secret.length()<40||secret.length()>100)throw new IllegalArgumentException("Autorizzazione fiscale non valida.");
        String saved=jobs.getString(id,null);JSONObject result;
        if(saved!=null){if(saved.isEmpty())throw new IllegalStateException("Emissione interrotta: verificare il DADO RT. Reinvio bloccato.");result=new JSONObject(saved);}
        else if("/fiscal/recover".equals(path))throw new IllegalStateException("Nessun esito su questo dispositivo. Usa il dispositivo di emissione e non reinviare.");
        else{
            JSONObject job=cloud("lavoro",new JSONObject().put("id",id),secret);
            jobs.edit().putString(id,"").commit();
            result=new JSONObject().put("id",id);
            try{
                JSONObject config=job.getJSONObject("config");
                if(!"axon_micrelec".equals(config.optString("brand")))throw new IllegalArgumentException("Su Android è attualmente supportato DADO RT / RT30.");
                result.put("axon",emit(config,job.getJSONObject("xml")));
            }catch(Exception error){result.put("error",safeMessage(error));}
            jobs.edit().putString(id,result.toString()).commit();
        }
        return cloud("esito",result,secret);
    }

    private JSONObject request(JSONObject config,String packet)throws Exception{
        validateConfig(config);String query=packet==null?"cmd=0":"cmd=4&js=1&pkt="+URLEncoder.encode(packet,"UTF-8");
        HttpURLConnection connection=(HttpURLConnection)new URL("http://"+config.getString("ip")+":"+number(config.opt("port"),1,65535)+"/_io?"+query).openConnection();
        connection.setConnectTimeout(15000);connection.setReadTimeout(15000);connection.setRequestProperty("Cache-Control","no-store");
        try{int status=connection.getResponseCode();byte[] raw=read(connection,status==200?connection.getInputStream():connection.getErrorStream(),65536);if(status!=200)throw new IllegalStateException("Risposta HTTP DADO RT non valida.");String decoded;
            try{decoded=new String(raw,StandardCharsets.UTF_8);new JSONObject(decoded);}catch(Exception ignored){decoded=new String(raw,Charset.forName("windows-1252"));}return new JSONObject(decoded);
        }finally{connection.disconnect();}
    }

    private CommandResult command(JSONObject config,String packet)throws Exception{
        String[] all=request(config,packet).optString("response").split("/",-1);if(all.length<4||all[0].length()!=2||all[1].length()!=2||all[2].length()!=2)throw new IllegalStateException("Risposta DADO RT incompleta.");
        int s0=Integer.parseInt(all[0],16),s1=Integer.parseInt(all[1],16),s2=Integer.parseInt(all[2],16);if(s0!=0)throw new IllegalStateException("DADO RT errore "+all[0]+" · comando "+packet.split("/",2)[0]+". Nessun reinvio automatico.");
        List<String> fields=new ArrayList<>();for(int i=3;i<all.length-1;i++)fields.add(all[i]);return new CommandResult(fields,s0,s1,s2);
    }
    private List<String> idle(JSONObject config,boolean closed)throws Exception{CommandResult value=command(config,"X/");if(value.fields.isEmpty()||!"0".equals(value.fields.get(0))||(value.s1&0xB7)!=0||(value.s2&0xFC)!=0)throw new IllegalStateException("DADO RT occupato, documento aperto o errore hardware.");if(closed&&(value.s2&2)!=0)throw new IllegalStateException("Chiudi prima la giornata fiscale sul DADO RT.");return value.fields;}

    private JSONObject emit(JSONObject config,JSONObject sale)throws Exception{
        List<String> before=idle(config,false);List<String> pay=command(config,"{/"+number(sale.opt("payment_index"),1,20)+"/").fields;String expected="contanti".equals(sale.optString("payment"))?"0":"1";
        if(pay.size()!=10||!expected.equals(pay.get(4))||!pay.get(6).startsWith("10")||!"0".equals(pay.get(3))||!"0".equals(pay.get(9)))throw new IllegalStateException("Totalizzatore DADO incompatibile: scegli un pagamento attivo senza POS integrato.");
        JSONArray departments=sale.getJSONArray("departments");for(int i=0;i<departments.length();i++){String dep=String.valueOf(number(departments.get(i),1,99));List<String> row=command(config,":/"+dep+"/").fields;if(row.size()<7||!row.get(6).startsWith("110")||!row.get(6).endsWith("0"))throw new IllegalStateException("Reparto "+dep+" non compatibile con la vendita diretta.");}
        JSONArray commands=sale.getJSONArray("commands");for(int i=0;i<commands.length();i++)command(config,commands.getString(i));
        List<String> after=idle(config,false);JSONObject snapshot=request(config,null),last=snapshot.getJSONObject("ej").getJSONObject("lastdoc");int beforeNo=Integer.parseInt(before.get(1)),afterNo=Integer.parseInt(after.get(1)),docNo=last.getInt("docno");
        if(afterNo!=beforeNo+1||docNo!=afterNo)throw new IllegalStateException("Numero documento DADO non confermato. Verifica manuale richiesta; non reinviare.");
        List<String> totals=command(config,"0/4/"+last.getInt("zno")+"/"+docNo+"/").fields;if(totals.size()!=31)throw new IllegalStateException("Totali documento DADO incompleti. Non reinviare.");BigDecimal total=BigDecimal.ZERO;for(int i=2;i<14;i++)total=total.add(new BigDecimal(totals.get(i)));
        if(total.compareTo(new BigDecimal(sale.getString("due")))!=0)throw new IllegalStateException("Totale DADO diverso dal pagamento. Non reinviare.");
        return new JSONObject().put("brand","axon_micrelec").put("before",beforeNo).put("number",afterNo).put("zno",last.getInt("zno")).put("date",snapshot.getJSONObject("rtc").optString("date")).put("serial",snapshot.getJSONObject("device").optString("serial")).put("amount",total.toPlainString());
    }

    private JSONObject readProgramming(JSONObject config,JSONObject batch)throws Exception{
        int start=number(batch==null?1:batch.opt("start"),1,99),count=number(batch==null?25:batch.opt("count"),1,25);boolean details=batch==null||batch.optBoolean("details",true);List<String> info=command(config,"v/").fields;if(info.size()<12)throw new IllegalStateException("Informazioni DADO incomplete.");int limit=number(info.get(4),1,99);
        JSONObject out=new JSONObject().put("departments",new JSONArray()).put("vat",new JSONArray()).put("payments",new JSONArray()).put("headers",new JSONArray()).put("logo",new JSONObject()).put("department_count",limit).put("printer",new JSONObject().put("model",config.optString("model")).put("ip",config.optString("ip")).put("serial","").put("firmware",info.get(0)));
        for(int n=start;n<Math.min(start+count,limit+1);n++){List<String> row=command(config,":/"+n+"/").fields;if(row.size()<7)throw new IllegalStateException("Reparto DADO incompleto.");out.getJSONArray("departments").put(new JSONObject().put("number",n).put("description",row.get(0).trim()).put("vat_group",row.get(1)).put("raw",new JSONArray(row)));}
        if(!details)return out;List<String> identity=command(config,"a/").fields;out.getJSONObject("printer").put("serial",identity.isEmpty()?"":identity.get(0));List<String> vat=command(config,"e/").fields;if(vat.size()!=36)throw new IllegalStateException("Tabella IVA DADO incompleta.");for(int i=0;i<12;i++)out.getJSONArray("vat").put(new JSONObject().put("group",String.valueOf(i+1)).put("rate",new BigDecimal(vat.get(i)).multiply(new BigDecimal("100")).stripTrailingZeros().toPlainString()).put("nature",vat.get(i+12)).put("ateco",vat.get(i+24)));
        int paymentCount=number(info.get(5),1,20);for(int n=1;n<=paymentCount;n++){List<String> row=command(config,"{/"+n+"/").fields;if(row.size()!=10)throw new IllegalStateException("Pagamento DADO incompleto.");out.getJSONArray("payments").put(new JSONObject().put("index",n).put("description",row.get(0).trim()).put("type",row.get(4)).put("raw",new JSONArray(row)));}
        List<String> headers=command(config,"O/").fields;if(headers.size()!=25)throw new IllegalStateException("Intestazione DADO incompleta.");for(int i=0;i<12;i++)out.getJSONArray("headers").put(new JSONObject().put("line",i+1).put("text",headers.get(i*2).trim()).put("font",Integer.parseInt(headers.get(i*2+1))).put("centered",false));return out;
    }

    private JSONObject writeProgramming(JSONObject config,JSONObject payload)throws Exception{
        if(!"SCRIVI CONFIGURAZIONE AXON".equals(payload.optString("confirmation")))throw new IllegalArgumentException("Conferma DADO mancante.");JSONArray sections=payload.optJSONArray("sections");JSONObject data=payload.optJSONObject("data");if(sections==null||sections.length()==0||data==null)throw new IllegalArgumentException("Sezioni DADO non valide.");List<String> packets=new ArrayList<>(),labels=new ArrayList<>();
        for(int s=0;s<sections.length();s++){String section=sections.getString(s);if("vat".equals(section)){JSONArray rows=data.getJSONArray("vat");if(rows.length()!=12)throw new IllegalArgumentException("Leggere prima tutta la tabella IVA.");List<String> values=new ArrayList<>();for(int i=0;i<12;i++)values.add(new BigDecimal(number(rows.getJSONObject(i).opt("rate"),0,9999)).divide(new BigDecimal("100")).setScale(2).toPlainString());for(int i=0;i<12;i++)values.add(new BigDecimal(values.get(i)).signum()==0?String.valueOf(number(rows.getJSONObject(i).opt("nature"),0,6)):"");for(int i=0;i<12;i++)values.add(String.valueOf(number(rows.getJSONObject(i).opt("ateco"),0,12)));packets.add("b/"+String.join("/",values)+"/");labels.add("IVA");}
            else if("departments".equals(section)){JSONArray rows=data.getJSONArray("departments");for(int i=0;i<rows.length();i++){JSONObject row=rows.getJSONObject(i);packets.add("N/"+number(row.opt("number"),1,99)+"/"+text(row.optString("description"),30)+"/"+number(row.opt("vat_group"),1,12)+"///////");labels.add("Reparto "+row.optInt("number"));}}
            else if("payments".equals(section)){JSONArray rows=data.getJSONArray("payments");for(int i=0;i<rows.length();i++){JSONObject row=rows.getJSONObject(i);packets.add("E/"+number(row.opt("index"),1,20)+"/"+text(row.optString("description"),25)+"//1.00//"+number(row.opt("type"),0,3)+"////");labels.add("Pagamento "+row.optInt("index"));}}
            else if("headers".equals(section)){JSONArray rows=data.getJSONArray("headers");if(rows.length()<8)throw new IllegalArgumentException("Intestazione DADO incompleta.");List<String> values=new ArrayList<>();values.add("L");for(int i=0;i<8;i++){JSONObject row=rows.getJSONObject(i);String value=text(row.optString("text"),48);int font=number(row.opt("font"),0,4);if((font==2||font==3)&&value.trim().length()>24)throw new IllegalArgumentException("Doppia larghezza: massimo 24 caratteri.");values.add(String.valueOf(font));values.add(value.isEmpty()?" ":value.trim());values.add(row.optBoolean("centered")?"0":"1");}packets.add(String.join("/",values)+"/");labels.add("Intestazione");}
            else throw new IllegalArgumentException("Sezione DADO non supportata: "+section);
        }
        idle(config,true);int written=0;for(int i=0;i<packets.size();i++){try{command(config,packets.get(i));written++;}catch(Exception e){throw new IllegalStateException(labels.get(i)+": "+safeMessage(e)+". Comandi già confermati: "+written+". Rileggi prima di riprovare.");}}
        return new JSONObject().put("ok",true).put("message","Configurazione DADO inviata. Rileggi per verificare.").put("sections",sections);
    }

    private JSONObject cloud(String path,JSONObject body,String secret)throws Exception{HttpURLConnection connection=(HttpURLConnection)new URL(BASE+"/api/fiscale/"+path).openConnection();connection.setRequestMethod("POST");connection.setDoOutput(true);connection.setConnectTimeout(20000);connection.setReadTimeout(20000);connection.setRequestProperty("Content-Type","application/json");connection.setRequestProperty("Authorization","Bearer "+secret);byte[] payload=body.toString().getBytes(StandardCharsets.UTF_8);connection.setFixedLengthStreamingMode(payload.length);try(OutputStream out=connection.getOutputStream()){out.write(payload);}int status=connection.getResponseCode();byte[] raw=read(connection,status<400?connection.getInputStream():connection.getErrorStream(),65536);JSONObject response=new JSONObject(new String(raw,StandardCharsets.UTF_8));if(status>=400)throw new IllegalStateException(response.optString("error",response.optString("message","Errore server fiscale.")));return response;}
    private void validateConfig(JSONObject config)throws Exception{if(!"axon_micrelec".equals(config.optString("brand"))||!"Dado RT / RT30".equals(config.optString("model")))throw new IllegalArgumentException("Configurazione DADO non valida.");byte[] ip=InetAddress.getByName(config.optString("ip")).getAddress();if(ip.length!=4)throw new IllegalArgumentException("IP DADO non valido.");int a=ip[0]&255,b=ip[1]&255;if(!(a==10||(a==172&&b>=16&&b<=31)||(a==192&&b==168)))throw new IllegalArgumentException("Il DADO deve avere un IP della rete locale.");}
    private int number(Object value,int low,int high){try{if(value instanceof Boolean)throw new Exception();int n=Integer.parseInt(String.valueOf(value));if(n<low||n>high)throw new Exception();return n;}catch(Exception e){throw new IllegalArgumentException("Valore DADO consentito: "+low+"-"+high+".");}}
    private String text(String value,int width){if(value.length()>width||value.indexOf('/')>=0)throw new IllegalArgumentException("Testo DADO: massimo "+width+" caratteri, senza /.");for(char c:value.toCharArray())if(c<32||c>126)throw new IllegalArgumentException("Testo DADO solo ASCII.");return value;}
    private byte[] read(HttpURLConnection connection,InputStream input,int max)throws Exception{if(input==null)return new byte[0];try(InputStream in=input;ByteArrayOutputStream out=new ByteArrayOutputStream()){byte[] buffer=new byte[4096];int count,total=0;while((count=in.read(buffer))!=-1){total+=count;if(total>max)throw new IllegalStateException("Risposta troppo grande.");out.write(buffer,0,count);}return out.toByteArray();}}
    private String safeMessage(Exception error){return error.getMessage()==null?error.getClass().getSimpleName():error.getMessage();}
    private static final class CommandResult{final List<String> fields;final int s0,s1,s2;CommandResult(List<String> f,int a,int b,int c){fields=f;s0=a;s1=b;s2=c;}}
}
