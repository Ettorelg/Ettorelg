package com.alphasystemsrl.alphamenu;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Intent;
import android.net.Uri;
import android.os.Environment;
import android.widget.Toast;
import org.json.JSONObject;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** Explicit, user-triggered updates from the Alpha Menu HTTPS origin. */
final class AppUpdates {
    private static final String ORIGIN="https://menu.alphasystemsrl.it";
    private final Activity activity;
    AppUpdates(Activity activity){this.activity=activity;}
    void show(){
        try{
            String installed=activity.getPackageManager().getPackageInfo(activity.getPackageName(),0).versionName;
            new AlertDialog.Builder(activity).setTitle("Aggiornamenti")
                .setMessage("Versione installata: "+installed+"\nCerca e scarica l’ultima versione di Alpha Menu.")
                .setPositiveButton("Cerca aggiornamenti",(d,w)->check())
                .setNeutralButton("Apri download",(d,w)->activity.startActivity(new Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)))
                .setNegativeButton("Chiudi",null).show();
        }catch(Exception error){message("Impossibile leggere la versione installata.");}
    }
    private void message(String text){activity.runOnUiThread(()->{if(!activity.isFinishing()&&!activity.isDestroyed())Toast.makeText(activity,text,Toast.LENGTH_LONG).show();});}
    private void check(){
        message("Ricerca aggiornamenti…");
        new Thread(()->{
            HttpURLConnection connection=null;
            try{
                connection=(HttpURLConnection)new URL(ORIGIN+"/static/android/latest.json?t="+System.currentTimeMillis()).openConnection();
                connection.setInstanceFollowRedirects(false);connection.setConnectTimeout(10000);connection.setReadTimeout(10000);
                if(connection.getResponseCode()!=200)throw new Exception("Server aggiornamenti non disponibile. Riprova più tardi.");
                byte[] bytes;try(java.io.InputStream input=connection.getInputStream();java.io.ByteArrayOutputStream output=new java.io.ByteArrayOutputStream()){
                    byte[] buffer=new byte[2048];int count;while((count=input.read(buffer))!=-1){output.write(buffer,0,count);if(output.size()>32768)break;}bytes=output.toByteArray();}
                if(bytes.length>32768)throw new Exception("Risposta aggiornamenti non valida.");
                JSONObject release=new JSONObject(new String(bytes,StandardCharsets.UTF_8));
                long version=release.getLong("versionCode");String name=release.getString("versionName"),path=release.getString("path");
                if(!path.matches("/static/android/AlphaMenu-[0-9.]+\\.apk"))throw new Exception("Indirizzo aggiornamento non valido.");
                long installed=activity.getPackageManager().getPackageInfo(activity.getPackageName(),0).getLongVersionCode();
                if(version<=installed){message("Alpha Menu è già aggiornato ("+name+").");return;}
                activity.runOnUiThread(()->{if(activity.isFinishing()||activity.isDestroyed())return;
                    new AlertDialog.Builder(activity).setTitle("Nuova versione "+name)
                        .setMessage(release.optString("notes","Aggiornamento Alpha Menu")+"\n\nAl termine apri il download e conferma l’installazione in Android.")
                        .setPositiveButton("Scarica",(d,w)->download(path,name)).setNegativeButton("Più tardi",null).show();});
            }catch(Exception error){message(error.getMessage()==null?"Ricerca aggiornamenti non riuscita.":error.getMessage());}
            finally{if(connection!=null)connection.disconnect();}
        },"alpha-update-check").start();
    }
    private void download(String path,String version){
        try{
            DownloadManager manager=activity.getSystemService(DownloadManager.class);
            DownloadManager.Request request=new DownloadManager.Request(Uri.parse(ORIGIN+path));
            request.setTitle("Alpha Menu "+version).setDescription("Apri per installare l’aggiornamento")
                .setMimeType("application/vnd.android.package-archive")
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS,"AlphaMenu-"+version+"-"+System.currentTimeMillis()+".apk");
            manager.enqueue(request);message("Download avviato. Aprilo dalla notifica o da Aggiornamenti → Apri download.");
        }catch(Exception error){message("Impossibile avviare il download: "+error.getMessage());}
    }
}
